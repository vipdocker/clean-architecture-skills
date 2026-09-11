#!/bin/bash
# Clean Architecture Skill & Agent System — Uninstall Script
# Usage: bash uninstall.sh
#
# Removes ONLY this project's skills and agents:
#   ~/.qoder/skills/<name>, ~/.agents/skills/<name>, ~/.qoderwork/skills/<name>
#   ~/.qoder/agents/<name>.md, ~/.qoderwork/agents/<name>.md

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_SRC="$SCRIPT_DIR/skills"
AGENT_SRC="$SCRIPT_DIR/agents"

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

if [ ! -d "$SKILL_SRC" ] || [ ! -d "$AGENT_SRC" ]; then
  echo "Error: run this script from the repo root (skills/ and agents/ must be present)."
  exit 1
fi

VERSION="$(sed -n 's/^<!-- clean-architecture system v\([0-9][0-9.]*\) -->$/\1/p' \
  "$SKILL_SRC/clean-architecture-autopilot/SKILL.md" 2>/dev/null | head -n1)"

echo -e "${CYAN}Clean Architecture Skill & Agent System v${VERSION:-unknown} — Uninstalling...${NC}"
echo ""

# ─── Remove skills from all three locations ───
for root in "$HOME/.qoder/skills" "$HOME/.agents/skills" "$HOME/.qoderwork/skills"; do
  pretty="${root/#$HOME/\~}"
  for d in "$SKILL_SRC"/*/; do
    [ -d "$d" ] || continue
    skill="$(basename "$d")"
    target="$root/$skill"
    if [ -L "$target" ] || [ -d "$target" ]; then
      echo -e "  Removing $pretty/$skill"
      rm -rf "$target"
    fi
  done
done

# ─── Remove pre-v1.9.0 skill names (no ca- prefix), ownership-guarded ───
LEGACY_SKILLS="use-case-extraction layer-boundaries dependency-rule solid-principles component-principles architecture-review-checklist process-tuning"
for legacy in $LEGACY_SKILLS; do
  for root in "$HOME/.qoder/skills" "$HOME/.agents/skills" "$HOME/.qoderwork/skills"; do
    t="$root/$legacy"
    if [ -d "$t" ] && [ ! -L "$t" ] && grep -q "clean-architecture system" "$t/SKILL.md" 2>/dev/null; then
      echo -e "  Removing ${root/#$HOME/\~}/$legacy (pre-v1.9.0 name)"
      rm -rf "$t"
    fi
  done
done

# ─── Remove agents: package subdirs + legacy flat copies ───
AGENT_PKG="clean-architecture-autopilot"

for root in "$HOME/.qoder/agents" "$HOME/.qoderwork/agents"; do
  pretty="${root/#$HOME/\~}"
  if [ -d "$root/$AGENT_PKG" ]; then
    echo -e "  Removing $pretty/$AGENT_PKG/"
    rm -rf "$root/$AGENT_PKG"
  fi
  for f in "$AGENT_SRC"/*.md; do
    [ -f "$f" ] || continue
    agent="$(basename "$f")"
    if [ -f "$root/$agent" ]; then
      echo -e "  Removing $pretty/$agent (legacy flat)"
      rm -f "$root/$agent"
    fi
  done
done

echo ""
echo -e "${GREEN}Uninstall complete. All skill and agent files removed.${NC}"
