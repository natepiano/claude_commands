execute the following instructions:

Run the bake_textures.py script through Blender to bake PBR textures using the provided configuration file.

Execute this bash command (the configuration file path is: $ARGUMENTS):

```bash
/opt/homebrew/bin/blender -b --python ~/.claude/scripts/bake_textures/bake_textures.py -- "$ARGUMENTS"
```