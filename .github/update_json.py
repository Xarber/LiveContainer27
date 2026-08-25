import argparse
import json
import os
import re
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import datetime, timezone


JSON_FILE = ".github/apps.json"
STATE_RELEASE_TAG = "1.0"

LIVE_CONTAINER_APP_ID = "com.kdt.livecontainer"
DEFAULT_APP_NAME = "LiveContainer + SideStore"

SIDESTORE_ASSET = "LiveContainer+SideStore.ipa"
STANDALONE_ASSET = "LiveContainer.ipa"

CHANNELS = (
    "stable",
    "nightly",
    "stable-standalone",
    "nightly-standalone",
)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Update the LiveContainer AltStore source."
    )
    parser.add_argument(
        "--repository",
        required=True,
        help="GitHub repository in owner/name format.",
    )
    return parser.parse_args()


ARGS = parse_arguments()

REPOSITORY = ARGS.repository
RELEASE_TAG = os.environ.get("RELEASE_TAG", "")
IS_NIGHTLY = (
    os.environ.get("IS_NIGHTLY", "true").strip().lower() != "false"
)
COMMIT_SHA = os.environ.get("COMMIT_SHA", "")
COMMIT_MESSAGE = os.environ.get("COMMIT_MESSAGE", "").strip()
WORKFLOW_URL = os.environ.get("WORKFLOW_URL", "")


def github_headers():
    token = os.environ.get("GITHUB_TOKEN")

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "LiveContainer-AltStore-Updater",
    }

    if token:
        headers["Authorization"] = f"Bearer {token}"

    return headers


def github_api_get(url):
    request = urllib.request.Request(
        url,
        headers=github_headers(),
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub API request failed with HTTP {error.code}: {body}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Unable to connect to GitHub API: {error}"
        ) from error


def github_download(url):
    request = urllib.request.Request(
        url,
        headers=github_headers(),
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub download failed with HTTP {error.code}: {body}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Unable to download from GitHub: {error}"
        ) from error


def prepare_description(text):
    if not text:
        return ""

    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r"\2",
        text,
    )
    text = re.sub(r"<[^<]+?>", "", text)
    text = re.sub(r"#{1,6}\s?", "", text)
    text = re.sub(r"\*{2}", "", text)
    text = re.sub(r"(?<=\r|\n)-", "•", text)
    text = re.sub(r"`", '"', text)
    text = re.sub(r"\r\n\r\n", "\r\n", text)

    return text.strip()


def load_base_json():
    with open(JSON_FILE, "r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise RuntimeError(
            f"{JSON_FILE} must contain a JSON object at the root."
        )

    return data


def load_persistent_json():
    release_url = (
        f"https://api.github.com/repos/{REPOSITORY}/releases/tags/"
        f"{STATE_RELEASE_TAG}"
    )

    try:
        release = github_api_get(release_url)
    except RuntimeError as error:
        if "HTTP 404" in str(error):
            print("No existing 1.0 release found.")
            return None
        raise

    for asset in release.get("assets", []):
        if asset.get("name") != "apps.json":
            continue

        download_url = asset.get("browser_download_url")
        if not download_url:
            raise RuntimeError(
                "The existing 1.0 release contains apps.json "
                "but has no download URL."
            )

        print("Loading current release state from 1.0.")
        raw_data = github_download(download_url)

        try:
            data = json.loads(raw_data.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "The apps.json asset in the 1.0 release is not valid JSON."
            ) from error

        if not isinstance(data, dict):
            raise RuntimeError(
                "The apps.json asset in the 1.0 release "
                "must contain a JSON object."
            )

        return data

    print("The 1.0 release contains no apps.json asset.")
    return None


def save_json(data):
    with open(JSON_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.write("\n")


def get_info_plist_versions():
    import plistlib

    with open("LiveContainer/Info.plist", "rb") as file:
        info = plistlib.load(file)

    version = info.get("CFBundleShortVersionString")
    build_version = info.get("CFBundleVersion")

    if not isinstance(version, str) or not version:
        raise RuntimeError(
            "LiveContainer/Info.plist has no valid "
            "CFBundleShortVersionString."
        )

    if not isinstance(build_version, str) or not build_version:
        raise RuntimeError(
            "LiveContainer/Info.plist has no valid CFBundleVersion."
        )

    return version, build_version


def get_release():
    if not RELEASE_TAG:
        raise RuntimeError("RELEASE_TAG is not set.")

    url = (
        f"https://api.github.com/repos/{REPOSITORY}/releases/tags/"
        f"{RELEASE_TAG}"
    )

    return github_api_get(url)


def get_asset(release, filename):
    for asset in release.get("assets", []):
        if asset.get("name") == filename:
            return asset

    raise RuntimeError(
        f"Asset {filename!r} was not found in release "
        f"{release.get('tag_name')!r}."
    )


def release_download_url(release, filename):
    asset = get_asset(release, filename)

    return asset["browser_download_url"], asset["size"]


def release_date(release):
    published_at = release.get("published_at") or release.get("created_at")

    if published_at:
        return published_at

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_release_description(release):
    if not IS_NIGHTLY:
        return prepare_description(release.get("body") or "")

    commit = COMMIT_SHA[:7] if COMMIT_SHA else "unknown"
    message = COMMIT_MESSAGE or "No commit message available."

    lines = [
        f"Nightly build from commit {commit}.",
        message,
    ]

    if COMMIT_SHA:
        lines.append(
            f"Commit: https://github.com/{REPOSITORY}/commit/{COMMIT_SHA}"
        )

    if WORKFLOW_URL:
        lines.append(f"Workflow: {WORKFLOW_URL}")

    return "\n\n".join(lines)


def channel_map(app):
    channels = app.get("releaseChannels")

    if not isinstance(channels, list):
        return {}

    return {
        channel.get("track"): channel
        for channel in channels
        if isinstance(channel, dict)
        and isinstance(channel.get("track"), str)
    }


def is_current_architecture(data):
    if not isinstance(data, dict):
        return False

    apps = data.get("apps")
    if not isinstance(apps, list) or len(apps) != 1:
        return False

    app = apps[0]
    if not isinstance(app, dict):
        return False

    if app.get("name") != DEFAULT_APP_NAME:
        return False

    if app.get("bundleIdentifier") != LIVE_CONTAINER_APP_ID:
        return False

    channels = channel_map(app)

    return all(track in channels for track in CHANNELS)


def clean_source_from_base(base, persistent):
    data = deepcopy(base)

    apps = data.get("apps")
    if not isinstance(apps, list) or len(apps) != 1:
        raise RuntimeError(
            f"{JSON_FILE} must contain exactly one canonical app."
        )

    app = apps[0]
    if not isinstance(app, dict):
        raise RuntimeError(
            f"{JSON_FILE} contains an invalid app object."
        )

    if app.get("name") != DEFAULT_APP_NAME:
        raise RuntimeError(
            f"{JSON_FILE} must name its only app {DEFAULT_APP_NAME!r}."
        )

    if app.get("bundleIdentifier") != LIVE_CONTAINER_APP_ID:
        raise RuntimeError(
            f"{JSON_FILE} must use {LIVE_CONTAINER_APP_ID!r}."
        )

    for key in (
        "version",
        "versionDate",
        "versionDescription",
        "downloadURL",
        "size",
    ):
        app.pop(key, None)

    app["versions"] = []
    app["releaseChannels"] = [
        {"track": track, "releases": []}
        for track in CHANNELS
    ]
    data["news"] = []

    if not is_current_architecture(persistent):
        if persistent is not None:
            print(
                "Ignoring legacy 1.0 state because it does not use "
                "the one-app architecture."
            )
        return data

    previous_app = persistent["apps"][0]
    previous_versions = previous_app.get("versions", [])

    if previous_versions and isinstance(previous_versions[0], dict):
        app["versions"] = [deepcopy(previous_versions[0])]

    previous_channels = channel_map(previous_app)
    current_channels = channel_map(app)

    for track in CHANNELS:
        previous_releases = previous_channels[track].get("releases", [])

        if previous_releases and isinstance(previous_releases[0], dict):
            current_channels[track]["releases"] = [
                deepcopy(previous_releases[0])
            ]

    return data


def make_version_entry(
    version,
    build_version,
    date,
    description,
    download_url,
    size,
    nightly=False,
):
    entry = {
        "version": version,
        "buildVersion": build_version,
        "date": date,
        "localizedDescription": description,
        "downloadURL": download_url,
        "size": size,
    }

    if nightly:
        entry["commit"] = COMMIT_SHA[:7]
        entry["headline"] = COMMIT_MESSAGE

    return entry


def set_channel_release(app, track, entry):
    channels = channel_map(app)

    if track not in channels:
        raise RuntimeError(f"Missing required release channel: {track}")

    channels[track]["releases"] = [entry]


def update_source():
    print(f"Repository: {REPOSITORY}")
    print(f"Release tag: {RELEASE_TAG}")
    print(f"Nightly: {IS_NIGHTLY}")

    base = load_base_json()
    persistent = load_persistent_json()
    data = clean_source_from_base(base, persistent)

    version, build_version = get_info_plist_versions()
    release = get_release()
    date = release_date(release)
    description = get_release_description(release)

    standalone_url, standalone_size = release_download_url(
        release,
        STANDALONE_ASSET,
    )
    sidestore_url, sidestore_size = release_download_url(
        release,
        SIDESTORE_ASSET,
    )

    standalone_entry = make_version_entry(
        version=version,
        build_version=build_version,
        date=date,
        description=description,
        download_url=standalone_url,
        size=standalone_size,
        nightly=IS_NIGHTLY,
    )
    sidestore_entry = make_version_entry(
        version=version,
        build_version=build_version,
        date=date,
        description=description,
        download_url=sidestore_url,
        size=sidestore_size,
        nightly=IS_NIGHTLY,
    )

    app = data["apps"][0]

    if IS_NIGHTLY:
        set_channel_release(app, "nightly", sidestore_entry)
        set_channel_release(
            app,
            "nightly-standalone",
            standalone_entry,
        )
    else:
        app["versions"] = [sidestore_entry]
        set_channel_release(app, "stable", sidestore_entry)
        set_channel_release(
            app,
            "stable-standalone",
            standalone_entry,
        )

    #! DELETE THIS TO RESTORE RELEASECHANNELS
    app.pop("releaseChannels", None)

    save_json(data)

    print()
    print("AltStore source updated successfully.")
    print(f"Version: {version} ({build_version})")
    print(f"Release: {RELEASE_TAG}")
    print(f"Track: {'nightly' if IS_NIGHTLY else 'stable'}")
    print(f"LiveContainer IPA: {standalone_url}")
    print(f"SideStore IPA: {sidestore_url}")


def main():
    try:
        update_source()
    except Exception as error:
        print(f"ERROR: {error}")
        raise


if __name__ == "__main__":
    main()
