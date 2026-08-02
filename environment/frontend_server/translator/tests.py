"""Test entry point for the Smallville Django frontend.

Behavior tests were split into cohesive modules:

- test_helpers: shared helpers and the backend lease mixin
- test_auth_settings_routes: auth, settings, and basic routes
- test_environment_input: environment input validation
- test_runtime_coordination: runtime markers, lease, and snapshots
- test_replay_persona: replay, checkpoint, and persona state
- test_filesystem_symlinks: filesystem and symlink safety
- test_simulator_scripts: Node simulator script parsing

Run the suite with: python manage.py test translator
"""
