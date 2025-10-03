#!/bin/bash

# Script to safely remove blender MCP server from ~/.claude.json
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
NC='\033[0m' # No Color

echo -e "${YELLOW}Removing blender MCP server from Claude configuration...${NC}"

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

# Check if blender server exists in config
if ! grep -q '"blender"' "$CONFIG_FILE"; then
    echo -e "${YELLOW}Warning: blender MCP server not found in configuration${NC}"
    echo "No changes made."
    exit 0
fi

# Use jq to remove the blender server from mcpServers
if command -v jq &> /dev/null; then
    # Use jq if available for clean JSON manipulation
    jq 'del(.mcpServers.blender)' "$CONFIG_FILE" > "${CONFIG_FILE}.tmp" && mv "${CONFIG_FILE}.tmp" "$CONFIG_FILE"
    echo -e "${GREEN}Successfully removed blender MCP server using jq${NC}"
else
    # Fallback to sed if jq is not available
    # This is more fragile but works for simple cases
    echo -e "${YELLOW}jq not found, using sed (less reliable)${NC}"
    
    # Create a temporary file
    TEMP_FILE=$(mktemp)
    
    # Use perl for multi-line pattern matching
    perl -0pe 's/,?\s*"blender"\s*:\s*\{[^}]*\}//g' "$CONFIG_FILE" > "$TEMP_FILE"
    
    # Clean up any double commas that might result
    sed 's/,,/,/g' "$TEMP_FILE" > "${TEMP_FILE}.2"
    
    # Remove trailing comma before closing brace if it exists
    sed 's/,\s*}/}/g' "${TEMP_FILE}.2" > "$CONFIG_FILE"
    
    rm -f "$TEMP_FILE" "${TEMP_FILE}.2"
    echo -e "${GREEN}Successfully removed blender MCP server using perl/sed${NC}"
fi

echo -e "${GREEN}✓ Backup saved to: $BACKUP_FILE${NC}"
echo -e "${GREEN}✓ Blender MCP server removed from configuration${NC}"
echo ""
echo "To restore from backup, run:"
echo "  cp $BACKUP_FILE $CONFIG_FILE"