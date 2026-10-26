# Sourced, never run; bash only. A run started before the delegate -> unit rename exports its
# variables as PLAN_DELEGATE_<name>, and the run folder as PLAN_DELEGATE_BOARD_DIR. Give each one
# its UNIT_* name when that is unset, so a script reads the new name only. Python readers use
# unit_runs.environ. Kept while such a run can still be open.
for _unit_env_former in ${!PLAN_DELEGATE_@}; do
    _unit_env_name="UNIT_${_unit_env_former#PLAN_DELEGATE_}"
    [[ "${_unit_env_former}" != PLAN_DELEGATE_BOARD_DIR ]] || _unit_env_name=UNIT_RUN_DIR
    [[ -n "${!_unit_env_name+set}" ]] || export "${_unit_env_name}=${!_unit_env_former}"
done
unset _unit_env_former _unit_env_name
