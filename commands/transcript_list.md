# List Recent Transcripts

List the 10 most recent transcript exports from ~/.claude/transcript-exports/ with a brief description of each.

## Instructions

1. Find the 10 most recent transcript files in ~/.claude/transcript-exports/ (*.json files, excluding *_metadata.json)
2. For each transcript file:
   - Show the filename and timestamp
   - Read the first few conversation turns to identify the main topic
   - Provide a 1-2 line summary of what was discussed
3. Order them from newest to oldest
4. Include whether it was an auto or manual compact based on the filename prefix

Format the output as a numbered list with clear descriptions.