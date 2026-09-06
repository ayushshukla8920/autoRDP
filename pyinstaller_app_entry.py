"""Unified entry point: one `autordp` binary that is CLI by default and the
desktop GUI when asked.

    autordp                 -> the headless CLI (menu / actions)
    autordp test            -> CLI, interactive prompt
    autordp repo <url> ...  -> CLI, type a codebase
    autordp --gui           -> launch the desktop GUI window
    autordp --gui --stop    -> just the floating STOP button

tkinter is imported only when a GUI flag is used, so the CLI path stays light
(and works on a headless server); the GUI needs a display.
"""

import sys

GUI_FLAGS = {"--gui", "--live", "--desktop"}


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in GUI_FLAGS:
        from rdpauto.gui import main as gui_main
        return gui_main(argv[1:])           # pass the rest (e.g. --stop) to the GUI
    from rdpauto.cli import main as cli_main
    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
