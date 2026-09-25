"""Runs first in every GitHub run. Makes sure every file of the agent was uploaded."""
import os, sys

REQUIRED = [
    "requirements.txt",
    "agent/__init__.py", "agent/__main__.py", "agent/agent_utils.py", "agent/associations.py", "agent/audit.py", "agent/blog.py",
    "agent/cities.py", "agent/config.py", "agent/directories.py", "agent/followups.py", "agent/geo.py", "agent/hiring.py",
    "agent/indexnow.py", "agent/logutil.py", "agent/niches.py", "agent/osm.py", "agent/overture.py", "agent/policy.py",
    "agent/portal.py", "agent/registries.py", "agent/scheduler.py", "agent/search_console.py", "agent/seo.py", "agent/slack.py",
    "agent/web.py", "agent/wordpress.py", "agent/writer.py",
    "agent/social.py", "agent/media/__init__.py", "agent/media/drive.py", "agent/media/branding.py", "agent/media/providers.py",
    "agent/media/quality.py",
]
missing = [f for f in REQUIRED if not os.path.isfile(f)]
if missing:
    print("PROBLEM: these files are missing from your GitHub repository:")
    for f in missing:
        print("   -", f)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("::error title=Files missing on GitHub::Missing: " + ", ".join(missing) + ". Unzip umair-agent.zip and upload the agent folder again (Add file > Upload files).")
    print("Fix: unzip umair-agent.zip on your computer, then in GitHub click Add file > Upload files and drag in the agent folder "
          "(and requirements.txt) again. Missing files inside a folder mean the upload skipped them - upload that folder again.")
    sys.exit(1)
print("All", len(REQUIRED), "agent files are present.")
if not os.path.isfile("assets/logo.png"):
    # not fatal (the lead agent does not need it) - only the social media images do
    print("WARNING: assets/logo.png is missing - the social media images need your logo there (or set LOGO_PATH).")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("::warning title=Logo missing::assets/logo.png was not found. Social media images need it.")
