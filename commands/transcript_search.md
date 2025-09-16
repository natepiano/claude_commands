# Search Transcript

Search a specific transcript file for information based on provided instructions.

## Usage

Use $ARGUMENTS to specify both the transcript filename and search instructions.
Format: `<filename> | <search instructions>`

Example: `auto_compact_20250916_151230.json | find all discussions about hooks and summarize`

## Instructions

1. Parse $ARGUMENTS to extract:
   - The transcript filename (before the | separator)
   - The search/analysis instructions (after the | separator)
2. Read the specified transcript file from ~/.claude/transcript-exports/
3. Analyze the transcript content according to the provided instructions
4. Return relevant findings, summaries, or specific information as requested

If the file doesn't exist, inform the user and suggest using @transcript_list to see available files.

## Arguments

$ARGUMENTS