"""``python -m fluent_data_studio``: the desktop application, or a headless command (``--help`` lists them)."""

import sys

_HEADLESS = ("profile", "ask", "sources", "generate-demo", "--help", "-h")


def _main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in _HEADLESS:
        from fluent_data_studio.cli import main
        return main()
    try:
        import PyQt5.QtWidgets  # noqa: F401  (the desktop interface needs Qt and both FluentQt editions)
        import fluentqt  # noqa: F401
        import fluentqt_pro  # noqa: F401
        from fluent_data_studio.ui.app import main
    except ImportError as exc:  # the engine and CLI run without the desktop interface and its frameworks
        print(f"The desktop interface is not available ({exc}). Headless commands still work:\n", file=sys.stderr)
        from fluent_data_studio.cli import main
        return main(["--help"])
    return main()


if __name__ == "__main__":
    sys.exit(_main())
