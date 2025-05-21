import os
import json
import logging
from typing import Dict, List, Optional, Any
import cx_Oracle
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import anthropic
import uvicorn

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(title="Oracle MCP Server")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Environment variables (set these in your deployment environment)
ORACLE_CONNECTION_STRING = os.environ.get("ORACLE_CONNECTION_STRING", "username/password@localhost:1521/servicename")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "your_api_key_here")

# Initialize Anthropic client
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Request models
class QueryRequest(BaseModel):
    query: str
    parameters: Optional[Dict[str, Any]] = None

class QueryResponse(BaseModel):
    response: str
    metadata: Optional[Dict[str, Any]] = None

# Oracle Database Helper
class OracleDBHelper:
    def __init__(self, connection_string):
        self.connection_string = connection_string
        self.connection = None
        self.cursor = None

    def connect(self):
        try:
            self.connection = cx_Oracle.connect(self.connection_string)
            self.cursor = self.connection.cursor()
            return True
        except cx_Oracle.Error as error:
            logger.error(f"Database connection error: {error}")
            return False

    def disconnect(self):
        if self.cursor:
            self.cursor.close()
        if self.connection:
            self.connection.close()

    def execute_query(self, query, params=None):
        try:
            if not self.connection or not self.cursor:
                if not self.connect():
                    return None

            if params:
                self.cursor.execute(query, params)
            else:
                self.cursor.execute(query)

            columns = [col[0] for col in self.cursor.description] if self.cursor.description else []

            results = []
            for row in self.cursor:
                results.append(dict(zip(columns, row)))

            return {"columns": columns, "data": results}

        except cx_Oracle.Error as error:
            logger.error(f"Query execution error: {error}")
            return {"error": str(error)}

    def get_schema_info(self, table_name=None):
        if not self.connection or not self.cursor:
            if not self.connect():
                return None

        if table_name:
            # Get schema for specific table
            self.cursor.execute("""
                SELECT column_name, data_type, data_length, nullable 
                FROM user_tab_columns 
                WHERE table_name = :table_name
                ORDER BY column_id
            """, {"table_name": table_name.upper()})
        else:
            # Get list of tables
            self.cursor.execute("""
                SELECT table_name 
                FROM user_tables 
                ORDER BY table_name
            """)

        columns = [col[0] for col in self.cursor.description]
        results = []
        for row in self.cursor:
            results.append(dict(zip(columns, row)))

        return results

# Create database helper instance
db_helper = OracleDBHelper(ORACLE_CONNECTION_STRING)

# Function to generate SQL from natural language
def generate_sql(query, schema_info=None):
    """Generate SQL based on natural language query using Claude"""
    system_prompt = """You are a helpful AI assistant trained to convert natural language questions into valid Oracle SQL queries.
    Your task is to analyze the user's question and respond with ONLY a valid SQL query.
    
    Rules:
    1. Return ONLY the SQL query, nothing else.
    2. Avoid adding any commentary or explanations.
    3. If you can't create a valid SQL query from the question, respond with: "UNABLE_TO_GENERATE_SQL"
    4. Use proper Oracle SQL syntax.
    5. Use reasonable limits on SELECT queries where appropriate.
    6. If there are ambiguities, make reasonable assumptions and proceed with generating SQL.
    """

    # Include schema information if available
    user_prompt = f"""Based on the following schema information:
    {json.dumps(schema_info, indent=2)}
    
    Generate a valid Oracle SQL query for this question: {query}
    """

    if not schema_info:
        user_prompt = f"Generate a valid Oracle SQL query for this question: {query}"

    try:
        response = client.messages.create(
            model="claude-3-5-sonnet-20240620",
            max_tokens=1000,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_prompt}
            ]
        )

        result = response.content[0].text.strip()

        # If Claude couldn't generate SQL, handle that
        if result == "UNABLE_TO_GENERATE_SQL":
            return None

        return result
    except Exception as e:
        logger.error(f"Error generating SQL: {e}")
        return None

# Function to process natural language with Claude
def process_with_claude(query, db_results=None):
    """Process natural language with Claude to generate a response"""
    system_prompt = """You are a helpful AI assistant that answers questions about database information.
    You will be provided with query results from an Oracle database.
    Your task is to interpret these results and provide a clear, natural language response.
    
    Rules:
    1. Be concise and accurate.
    2. If the data shows no results, state that clearly.
    3. Format numerical data appropriately.
    4. Point out any limitations in the data if relevant.
    5. Do not invent data that isn't present in the results.
    """

    user_prompt = f"""
    User query: {query}
    
    Database results:
    {json.dumps(db_results, indent=2) if db_results else "No database results available"}
    
    Please provide a clear response based on this information.
    """

    try:
        response = client.messages.create(
            model="claude-3-5-sonnet-20240620",
            max_tokens=1000,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_prompt}
            ]
        )

        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"Error processing with Claude: {e}")
        return f"Error processing request: {str(e)}"

# Main route for handling queries
@app.post("/query", response_model=QueryResponse)
async def handle_query(request: QueryRequest):
    query_text = request.query

    # Step 1: Get schema information
    schema_info = db_helper.get_schema_info()
    if not schema_info:
        return {"response": "Unable to retrieve database schema information. Please check database connection."}

    # Step 2: Analyze if the query appears to be a database question
    # This is a simple heuristic; in production, you might use a classifier
    db_keywords = ["data", "database", "table", "record", "user", "customer", "information",
                  "how many", "list", "show", "find", "select", "get", "count"]

    is_db_query = any(keyword in query_text.lower() for keyword in db_keywords)

    if is_db_query:
        # Step 3: Generate SQL from the natural language query
        sql_query = generate_sql(query_text, schema_info)

        # Step 4: Execute the SQL if generation was successful
        if sql_query:
            try:
                db_results = db_helper.execute_query(sql_query)

                # Check for errors in execution
                if "error" in db_results:
                    response = f"There was an error executing the database query: {db_results['error']}"
                else:
                    # Process the results with Claude
                    response = process_with_claude(query_text, db_results)

                return {"response": response, "metadata": {"executed_sql": sql_query, "raw_results": db_results}}

            except Exception as e:
                logger.error(f"Error executing query: {e}")
                return {"response": f"Error executing database query: {str(e)}"}
        else:
            # If SQL generation failed, fall back to general Claude response
            return {"response": "I couldn't translate your question into a database query. Could you please rephrase it or provide more specific details?"}

    # For non-database queries, process directly with Claude
    response = process_with_claude(query_text)
    return {"response": response}

# Health check endpoint
@app.get("/health")
async def health_check():
    # Check database connection
    db_status = "OK" if db_helper.connect() else "ERROR"
    if db_helper.connection:
        db_helper.disconnect()

    return {
        "status": "healthy",
        "database": db_status,
        "api_version": "1.0"
    }

# Tables information endpoint
@app.get("/tables")
async def get_tables():
    tables = db_helper.get_schema_info()
    return {"tables": tables}

# Table details endpoint
@app.get("/tables/{table_name}")
async def get_table_details(table_name: str):
    table_details = db_helper.get_schema_info(table_name)
    if not table_details:
        raise HTTPException(status_code=404, detail=f"Table {table_name} not found")
    return {"table": table_name, "columns": table_details}

# Main entry point
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)