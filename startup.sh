#!/bin/bash
# Install Microsoft ODBC Driver 18 for SQL Server (needed for Fabric SQL endpoint queries)
if ! dpkg -s msodbcsql18 > /dev/null 2>&1; then
    echo "Installing ODBC Driver 18..."
    apt-get update -qq
    ACCEPT_EULA=Y DEBIAN_FRONTEND=noninteractive apt-get install -y -qq msodbcsql18 > /dev/null 2>&1
    echo "ODBC Driver 18 installed."
else
    echo "ODBC Driver 18 already installed."
fi

# Start the app
exec gunicorn -k uvicorn.workers.UvicornWorker --bind=0.0.0.0:8000 --timeout 120 app:app
