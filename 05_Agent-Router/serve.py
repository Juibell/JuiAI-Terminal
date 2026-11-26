import uvicorn

from jui.config import load_settings

if __name__ == "__main__":
    from jui.web import app

    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=load_settings()["port"], access_log=False
        )
    )
    app.state.server = server
    server.run()
