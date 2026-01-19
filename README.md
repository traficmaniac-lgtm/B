# BBOT Desktop Terminal (Stage 1.2)

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt
```

## Run GUI

```powershell
python -m src.app.main
```

### Pair Workspace

Double click any row in the Markets tab to open a Pair Workspace window for that symbol.

## Run tests

```powershell
pytest -q
```
