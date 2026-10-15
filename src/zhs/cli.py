"""Argument parsing without configuration, login or network side effects."""

import argparse
import math

from zhs import __version__


def parse_bool(value):
    if isinstance(value, bool):
        return value
    if value.lower() in {"true", "1", "yes", "on"}:
        return True
    if value.lower() in {"false", "0", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError("expected true/false, yes/no or 1/0")


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or a positive integer")
    return number


def boolean_option(parser, name, help_text, *aliases):
    destination = name.replace("-", "_")
    group = parser.add_mutually_exclusive_group()
    names = list(dict.fromkeys([f"--{name}", f"--{destination}", *aliases]))
    group.add_argument(
        *names,
        dest=destination,
        nargs="?",
        const=True,
        default=None,
        type=parse_bool,
        help=help_text,
    )
    group.add_argument(f"--no-{name}", dest=destination, action="store_false")


def build_parser():
    parser = argparse.ArgumentParser(prog="zhs", description="ZHS Toolkit 课程命令行工具")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--course", nargs="+", help="Course IDs")
    parser.add_argument("-v", "--videos", nargs="+", help="Video IDs; requires --course")
    parser.add_argument("-u", "--username")
    parser.add_argument("-p", "--password")
    parser.add_argument("-s", "--speed", type=positive_float)
    parser.add_argument("-t", "--threshold", type=positive_float)
    parser.add_argument(
        "-l",
        "--limit",
        type=nonnegative_int,
        default=0,
        help="Per-course limit in minutes (0: unlimited)",
    )
    boolean_option(parser, "qrlogin", "Use QR login", "-q")
    boolean_option(parser, "tree-view", "Print course tree")
    boolean_option(parser, "progressbar-view", "Print progress bar")
    boolean_option(parser, "show-in-terminal", "Print QR in terminal")
    parser.add_argument("-d", "--debug", action="store_true")
    parser.add_argument("-f", "--fetch", action="store_true", help="Save all course types")
    parser.add_argument("--proxy")
    parser.add_argument("--image_path", "--image-path", dest="image_path")
    parser.add_argument("-ai", "--aicourse", nargs=2, metavar=("COURSE_ID", "CLASS_ID"))
    exams = parser.add_mutually_exclusive_group()
    exams.add_argument(
        "--noexam",
        nargs="?",
        const=True,
        default=False,
        type=parse_bool,
        help="Skip AI exams (also accepts true/false)",
    )
    exams.add_argument("--exam", dest="noexam", action="store_false")
    parser.add_argument(
        "--check", action="store_true", help="Login and list courses without changing study records"
    )
    return parser
