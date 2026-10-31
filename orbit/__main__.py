from .config import Settings
import uvicorn


def main():
    settings = Settings.from_environment()
    uvicorn.run("orbit.app:app", host=settings.bind, port=settings.port, reload=False, workers=1)


if __name__ == "__main__":
    main()
