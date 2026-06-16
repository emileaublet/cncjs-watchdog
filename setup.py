from setuptools import setup

APP = ["run_app.py"]
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "CNCjs Watchdog",
        "LSUIElement": True,   # menu bar only — no Dock icon
    },
    "packages": ["rumps", "jwt", "websocket", "cncwatch"],
}

setup(
    name="CNCjs Watchdog",
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
