"""Packaged T5 CLI entry point."""


def main(argv=None):
    from agent_adapters.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
