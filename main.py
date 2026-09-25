import uvicorn

from app import config

if __name__ == "__main__":
    uvicorn.run("app.server:app", host="0.0.0.0", port=config.PORT)
