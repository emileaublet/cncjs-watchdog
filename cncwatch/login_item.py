import os
from xml.sax.saxutils import escape

LABEL = "com.emileaublet.cncjs-watchdog"
PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def _plist_xml(program_args):
    args = "".join(f"    <string>{escape(a)}</string>\n" for a in program_args)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        f'  <key>Label</key><string>{LABEL}</string>\n'
        '  <key>ProgramArguments</key>\n'
        f'  <array>\n{args}  </array>\n'
        '  <key>RunAtLoad</key><true/>\n'
        '</dict>\n'
        '</plist>\n'
    )


def is_enabled(path=PLIST_PATH):
    return os.path.exists(path)


def enable(program_args, path=PLIST_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(_plist_xml(program_args))


def disable(path=PLIST_PATH):
    if os.path.exists(path):
        os.remove(path)
