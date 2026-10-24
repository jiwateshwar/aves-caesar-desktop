"""Read-only SQL views for other applications that query the database directly.

The views are defined by the migrations (aves_server/migrations/versions), which version them as a
contract: 0001 created them, 0002 added columns for external apps. Change them with a new migration.
"""

VIEW_NAMES = ("v_published_photos", "v_species_sightings")
