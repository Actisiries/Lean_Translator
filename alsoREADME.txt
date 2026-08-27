HTTP mode from backend connected LLM:
	$env:LLM_BACKEND = "http"
	$env:LLM_HTTP_URL = "http://127.0.0.1:<port_number>/v1"
	$env:LLM_HTTP_MODEL = "<model_name>"

e.g.
	$env:LLM_BACKEND = "http"
	$env:LLM_HTTP_URL = "http://127.0.0.1:15721/v1"
	$env:LLM_HTTP_MODEL = "deepseek-v4-flash"

Close web server:
	netstat -ano | findstr <port_number>
	taskkill /F /PID <PID>

First time running:
	Get into Lean_work_1\mathlib_project in cmd/powershell and enter:
		lake update
		lake build
	
		If cant, maybe try following first (the exclamation mark is needed):
			lake clean
			lake exe cache clean!
			lake exe cache get
			lake build
	
	Click start_translator.bat in Lean_work_1\lean_formalizer

