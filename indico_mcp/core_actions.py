"""Register tools for features shipped with Indico."""

from indico_mcp import core, core_abstracts, core_account, core_materials, core_schedule, core_surveys


def register_tools(registry):
    core.register_tools(registry)
    core_schedule.register_tools(registry)
    core_materials.register_tools(registry)
    core_abstracts.register_tools(registry)
    core_account.register_tools(registry)
    core_surveys.register_tools(registry)
