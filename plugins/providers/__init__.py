"""Drop site for external AIOS HUD/runtime plugins.

Modules in this package may expose a module-level PLUGIN instance implementing
AIOSPlugin. Discovery is automatic; plugins remain responsible for declaring
whether they are enabled and whether they apply to a runtime context.
"""
