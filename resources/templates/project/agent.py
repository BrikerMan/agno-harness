"""Process entry. Serves FastAPI on port 8000.

Web is POST /api/v1/channels/web/agui. Teams is POST /api/v1/channels/teams/messages when that channel is enabled.
An enabled channel with missing settings logs an error and the process stops.
The terminal entry is python -m app.cli.
"""

import uvicorn
from app.main import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=8000)
