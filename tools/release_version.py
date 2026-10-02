"""Select reserved release versions from Git tags; no external Python packages."""

import argparse
import json
import os
import re
import subprocess

PATTERN = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-dev\.([1-9]\d*))?")


def parse(tag):
    match = PATTERN.fullmatch(tag)
    return tuple(int(value) if value is not None else 0 for value in match.groups()) if match else None


def select(branch, tags, commit):
    channel = "latest" if branch == "main" else "dev"
    versions = [(tag, sha, parse(tag)) for tag, sha in tags.items() if parse(tag)]
    existing = [(version, tag) for tag, sha, version in versions
                if sha == commit and bool(version[3]) == (branch == "develop")]
    if existing:
        return max(existing)[1], channel, True
    stable = max((version[:3] for _, _, version in versions if not version[3]), default=None)
    base = (stable[0], stable[1], stable[2] + 1) if stable else (0, 1, 0)
    tag = "v" + ".".join(map(str, base))
    if branch == "develop":
        number = max((version[3] for _, _, version in versions if version[:3] == base), default=0) + 1
        tag += f"-dev.{number}"
    return tag, channel, False


def release_state(tag, releases):
    releases = [release for release in releases if not release["draft"]]
    complete = any(release["tag_name"] == tag for release in releases)
    version = parse(tag)
    newer = any(parse(release["tag_name"]) and parse(release["tag_name"]) > version
                and bool(parse(release["tag_name"])[3]) == bool(version[3]) for release in releases)
    return complete, not newer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("branch", choices=("main", "develop"))
    parser.add_argument("commit")
    args = parser.parse_args()
    tags = {}
    for tag in subprocess.check_output(["git", "tag", "--list"], text=True).splitlines():
        if parse(tag):
            tags[tag] = subprocess.check_output(["git", "rev-list", "-n", "1", tag], text=True).strip()
    tag, channel, reused = select(args.branch, tags, args.commit)
    # Query releases independently of tags: a reserved tag may have an unfinished build.
    pages = subprocess.check_output(["gh", "api", "--paginate", "--slurp",
                                     f"repos/{os.environ['GITHUB_REPOSITORY']}/releases"], text=True)
    complete, promote = release_state(tag, [release for page in json.loads(pages) for release in page])
    values = {"tag": tag, "version": tag[1:], "channel": channel, "reused": str(reused).lower(),
              "complete": str(complete).lower(), "promote": str(promote).lower()}
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
