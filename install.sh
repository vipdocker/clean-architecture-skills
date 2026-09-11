#!/bin/bash
# Clean Architecture Skill & Agent System — Install Script
# Usage: bash install.sh
#
# Installs:
#   skills  → ~/.agents/skills/<name>          (real copy, Qoder reads this)
#             ~/.qoderwork/skills/<name>       (symlink → ~/.agents/skills/<name>)
#   agents  → ~/.qoder/agents/clean-architecture-autopilot/<name>.md
#             ~/.qoderwork/agents/clean-architecture-autopilot/<name>.md
#             (both use the per-package subdirectory layout; agents no longer
#             live flat in the agents root)
#
# Legacy cleanup: removes skills previously copied to ~/.qoder/skills/<name>
# and agents previously copied flat to ~/.qoder/agents/<name>.md or
# ~/.qoderwork/agents/<name>.md — Qoder reads both old and new locations, so
# stale copies there list everything twice.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_SRC="$SCRIPT_DIR/skills"
AGENT_SRC="$SCRIPT_DIR/agents"
AGENT_PKG="clean-architecture-autopilot"

GREEN='\033[0;32m'
RED='\033[0;31m'
CYAN='\033[0;36m'
YELLOW='\033[0;33m'
NC='\033[0m'

ERRORS=0

# ─── Collect package contents ───
if [ ! -d "$SKILL_SRC" ] || [ ! -d "$AGENT_SRC" ]; then
  echo -e "${RED}Error: skills/ or agents/ not found. Run this script from the repo root.${NC}"
  exit 1
fi

SKILLS=()
for d in "$SKILL_SRC"/*/; do
  [ -d "$d" ] || continue
  SKILLS+=("$(basename "$d")")
done

AGENTS=()
for f in "$AGENT_SRC"/*.md; do
  [ -f "$f" ] || continue
  AGENTS+=("$(basename "$f")")
done

if [ "${#SKILLS[@]}" -eq 0 ] || [ "${#AGENTS[@]}" -eq 0 ]; then
  echo -e "${RED}Error: no skills or agents found under $SCRIPT_DIR.${NC}"
  exit 1
fi

# ─── Pre-flight checks ───
VERSION="$(sed -n 's/^<!-- clean-architecture system v\([0-9][0-9.]*\) -->$/\1/p' \
  "$SKILL_SRC/clean-architecture-autopilot/SKILL.md" 2>/dev/null | head -n1)"

echo -e "${CYAN}Clean Architecture Skill & Agent System v${VERSION:-unknown} — Installing...${NC}"
echo ""

echo -e "${CYAN}[Pre-flight]${NC}"

for skill in "${SKILLS[@]}"; do
  if [ ! -f "$SKILL_SRC/$skill/SKILL.md" ]; then
    echo -e "  ${RED}Error: skills/$skill/SKILL.md missing.${NC}"
    exit 1
  fi
done
echo -e "  Skills:  ${#SKILLS[@]} (${SKILLS[*]})"

# The orchestrator's scripts are the mechanical authority (cc_log.py gates);
# shipping without them produces a broken pipeline.
for py in cc_log.py dep_graph.py design_coverage.py plan_graph.py; do
  if [ ! -f "$SKILL_SRC/clean-architecture-autopilot/scripts/$py" ]; then
    echo -e "  ${RED}Error: clean-architecture-autopilot/scripts/$py missing.${NC}"
    exit 1
  fi
done
echo -e "  Orchestrator scripts: ${GREEN}4/4${NC}"

# Version consistency: every skill comment and agent frontmatter must carry
# the same version. Refusing drifted files prevents the stale-copy failure
# (a run executing an older installed skill than the repo it came from).
DRIFT=0
for skill in "${SKILLS[@]}"; do
  v="$(sed -n 's/^<!-- clean-architecture system v\([0-9][0-9.]*\) -->$/\1/p' \
    "$SKILL_SRC/$skill/SKILL.md" | head -n1)"
  if [ "$v" != "$VERSION" ]; then
    echo -e "  ${RED}Error: skills/$skill version '$v' != '$VERSION'.${NC}"
    DRIFT=1
  fi
done
for agent in "${AGENTS[@]}"; do
  v="$(sed -n 's/^version:[[:space:]]*\([0-9][0-9.]*\)[[:space:]]*$/\1/p' \
    "$AGENT_SRC/$agent" | head -n1)"
  if [ "$v" != "$VERSION" ]; then
    echo -e "  ${RED}Error: agents/$agent version '$v' != '$VERSION'.${NC}"
    DRIFT=1
  fi
done
if [ "$DRIFT" -ne 0 ]; then
  echo -e "${RED}Refusing to install version-drifted files. Fix versions first.${NC}"
  exit 1
fi
echo -e "  Version consistency: ${GREEN}${VERSION} across ${#SKILLS[@]} skills + ${#AGENTS[@]} agents${NC}"
echo ""

# ─── Install skills → ~/.agents/skills (real copies) ───
echo -e "${CYAN}[Skills → ~/.agents/skills/]${NC}"

for root in "$HOME/.agents/skills"; do
  mkdir -p "$root"
  for skill in "${SKILLS[@]}"; do
    rsync -a --delete \
      --exclude '__pycache__' --exclude '.DS_Store' \
      "$SKILL_SRC/$skill/" "$root/$skill/"
    # excluded patterns survive --delete on the receiver; purge them explicitly
    # (nested too: scripts/__pycache__)
    find "$root/$skill" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null
    find "$root/$skill" -name '.DS_Store' -delete
  done
  OK=0
  for skill in "${SKILLS[@]}"; do
    [ -f "$root/$skill/SKILL.md" ] && OK=$((OK + 1))
  done
  if [ "$OK" -eq "${#SKILLS[@]}" ]; then
    echo -e "  $root/: ${GREEN}$OK/${#SKILLS[@]}${NC} skills"
  else
    echo -e "  $root/: ${RED}$OK/${#SKILLS[@]}${NC} skills"
    ERRORS=$((ERRORS + 1))
  fi
done

# Verify the load-bearing scripts landed
for root in "$HOME/.agents/skills"; do
  for py in cc_log.py dep_graph.py design_coverage.py plan_graph.py; do
    if [ ! -f "$root/clean-architecture-autopilot/scripts/$py" ]; then
      echo -e "  ${RED}Error: $root/clean-architecture-autopilot/scripts/$py missing after copy.${NC}"
      ERRORS=$((ERRORS + 1))
    fi
  done
done
echo ""

# ─── Legacy cleanup: ~/.qoder/skills copies list skills twice ───
for skill in "${SKILLS[@]}"; do
  target="$HOME/.qoder/skills/$skill"
  if [ -L "$target" ] || [ -d "$target" ]; then
    echo -e "  ${YELLOW}Removing legacy copy: ~/.qoder/skills/$skill${NC}"
    rm -rf "$target"
  fi
done
echo ""

# ─── Symlink ~/.qoderwork/skills/<name> → ~/.agents/skills/<name> ───
echo -e "${CYAN}[Symlinks → ~/.qoderwork/skills/]${NC}"

mkdir -p "$HOME/.qoderwork/skills"
for skill in "${SKILLS[@]}"; do
  target="$HOME/.qoderwork/skills/$skill"
  if [ -L "$target" ]; then
    rm -f "$target"
  elif [ -d "$target" ]; then
    # stale real copy from a previous manual install — replace with symlink
    echo -e "  ${YELLOW}Replacing existing directory: ~/.qoderwork/skills/$skill${NC}"
    rm -rf "$target"
  fi
  ln -s "$HOME/.agents/skills/$skill" "$target"
done

LINK_OK=0
for skill in "${SKILLS[@]}"; do
  [ -f "$HOME/.qoderwork/skills/$skill/SKILL.md" ] && LINK_OK=$((LINK_OK + 1))
done
if [ "$LINK_OK" -eq "${#SKILLS[@]}" ]; then
  echo -e "  Symlinks resolve: ${GREEN}$LINK_OK/${#SKILLS[@]}${NC} → ~/.agents/skills/"
else
  echo -e "  Symlinks resolve: ${RED}$LINK_OK/${#SKILLS[@]}${NC}"
  ERRORS=$((ERRORS + 1))
fi
echo ""

# ─── Install agents → package subdirectories in both locations ───
echo -e "${CYAN}[Agents → ~/.qoder/agents/$AGENT_PKG/ + ~/.qoderwork/agents/$AGENT_PKG/]${NC}"

for root in "$HOME/.qoder/agents" "$HOME/.qoderwork/agents"; do
  mkdir -p "$root/$AGENT_PKG"
  for agent in "${AGENTS[@]}"; do
    cp -f "$AGENT_SRC/$agent" "$root/$AGENT_PKG/$agent"
    # flat copy from older script versions duplicates the packaged one
    rm -f "$root/$agent"
  done
  OK=0
  for agent in "${AGENTS[@]}"; do
    [ -f "$root/$AGENT_PKG/$agent" ] && OK=$((OK + 1))
  done
  if [ "$OK" -eq "${#AGENTS[@]}" ]; then
    echo -e "  $root/$AGENT_PKG/: ${GREEN}$OK/${#AGENTS[@]}${NC} agents"
  else
    echo -e "  $root/$AGENT_PKG/: ${RED}$OK/${#AGENTS[@]}${NC} agents"
    ERRORS=$((ERRORS + 1))
  fi
done
echo ""

# ─── Summary ───
if [ "$ERRORS" -eq 0 ]; then
  echo -e "${GREEN}Installation complete (v${VERSION}).${NC}"
  echo ""
  echo "  Skills (real copies):  ~/.agents/skills/           (${#SKILLS[@]} dirs)"
  echo "  Skills (symlinks):     ~/.qoderwork/skills/        → ~/.agents/skills/"
  echo "  Agents:                ~/.qoder/agents/$AGENT_PKG/ (${#AGENTS[@]} files)"
  echo "  Agents (mirror):       ~/.qoderwork/agents/$AGENT_PKG/ (${#AGENTS[@]} files)"
  echo ""
  echo "  Orchestrator trigger: clean-architecture-autopilot"
  echo "  Re-run after every repo update to keep all locations in sync."
  echo ""
  echo "  Uninstall: bash uninstall.sh"
else
  echo -e "${RED}Installation finished with $ERRORS error(s). Check output above.${NC}"
  exit 1
fi
