execute the following instructions:

Run the bake_textures.py script through Blender to bake PBR textures using the provided configuration file.

Execute this bash command (the configuration file path is: $ARGUMENTS):

```bash
cd /Users/natemccoy/rust/hana-brp-extras-2-1 && /opt/homebrew/bin/blender -b --python /Users/natemccoy/rust/.claude/commands/blender/bake_textures.py -- "$ARGUMENTS"
```