**IMPORTANT** don't commit any changes. Just do the following:

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**
    **STEP 1:** Execute <DetectProject/>
    **STEP 2:** Execute <AddExclusion/>
</ExecutionSteps>

<DetectProject>
Determine the project name from the current working directory:
- Read the Cargo.toml in the current working directory to get the package name
- If no Cargo.toml exists, inform the user and stop

Store the package name for use in <AddExclusion/>.
</DetectProject>

<AddExclusion>
Read `~/.claude/config/cargo-fmt-exclusions.json` and check if the project name is already present.

If already present:
- Inform the user: "`[name]` is already in cargo-fmt exclusions"
- Stop

If not present:
- Add the project name to the JSON array in sorted order
- Write the updated file
- Inform the user: "Added `[name]` to cargo-fmt exclusions"
</AddExclusion>
