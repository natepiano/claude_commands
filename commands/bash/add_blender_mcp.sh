#!/bin/bash

# Script to safely add blender MCP server to ~/.claude.json
# Creates a backup before making changes

set -euo pipefail

CONFIG_FILE="$HOME/.claude.json"
BACKUP_DIR="$HOME/.claude_backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/claude.json.bak_${TIMESTAMP}"

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${YELLOW}Adding blender MCP server to Claude configuration...${NC}"

# Check if config file exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo -e "${RED}Error: $CONFIG_FILE not found${NC}"
    exit 1
fi

# Create backup directory if it doesn't exist
mkdir -p "$BACKUP_DIR"

# Create backup
echo -e "${GREEN}Creating backup at: $BACKUP_FILE${NC}"
cp "$CONFIG_FILE" "$BACKUP_FILE"

# Check if blender server already exists in config
if grep -q '"blender"' "$CONFIG_FILE"; then
    echo -e "${YELLOW}Warning: blender MCP server already exists in configuration${NC}"
    echo -e "${BLUE}Current blender configuration:${NC}"
    # Extract and display the blender configuration
    if command -v jq &> /dev/null; then
        jq '.mcpServers.blender' "$CONFIG_FILE" 2>/dev/null || echo "Could not parse blender config"
    else
        grep -A 5 '"blender"' "$CONFIG_FILE" || echo "Could not find blender config"
    fi
    echo ""
    read -p "Do you want to replace it? (y/n): " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted. No changes made."
        exit 0
    fi
fi

# Define the blender MCP server configuration
BLENDER_CONFIG='{
  "command": "uvx",
  "args": ["blender-mcp"]
}'

# Use jq to add the blender server to mcpServers
if command -v jq &> /dev/null; then
    # Use jq if available for clean JSON manipulation
    jq --argjson blender "$BLENDER_CONFIG" '.mcpServers.blender = $blender' "$CONFIG_FILE" > "${CONFIG_FILE}.tmp" && mv "${CONFIG_FILE}.tmp" "$CONFIG_FILE"
    echo -e "${GREEN}Successfully added blender MCP server using jq${NC}"
else
    # Fallback to more complex manipulation without jq
    echo -e "${YELLOW}jq not found, using alternative method${NC}"
    echo -e "${RED}Manual addition required. Please add the following to your mcpServers section:${NC}"
    echo ""
    echo '  "blender": {'
    echo '    "command": "uvx",'
    echo '    "args": ["blender-mcp"]'
    echo '  }'
    echo ""
    echo "Backup has been created at: $BACKUP_FILE"
    exit 1
fi

echo -e "${GREEN}✓ Backup saved to: $BACKUP_FILE${NC}"
echo -e "${GREEN}✓ Blender MCP server added to configuration${NC}"
echo ""
echo -e "${BLUE}Blender MCP configuration:${NC}"
echo '  "blender": {'
echo '    "command": "uvx",'
echo '    "args": ["blender-mcp"]'
echo '  }'
echo ""
echo "To restore from backup, run:"
echo "  cp $BACKUP_FILE $CONFIG_FILE"
echo ""
echo -e "${YELLOW}Note: You may need to restart Claude for the changes to take effect.${NC}"