@echo off
rem Oracle Edge: read-only data-gap report. Writes C:\OracleEdge\logs\data_gaps.json
"C:\OracleEdge\runtime\venv\Scripts\python.exe" "%~dp0data_gaps.py" > "C:\OracleEdge\logs\data_gaps_run.log" 2>&1
