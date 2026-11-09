"""Construct historical fixtures by removing newer schema, never production downgrade."""


def remove_full_analysis_schema(connection):
    # Historical <=v14 fixtures must also remove the newer v16 recycle-bin
    # table; dropping it removes its own journal triggers, as on a real old DB.
    connection.exec_driver_sql('DROP TABLE IF EXISTS domain_trash')
    for name in ('pka_full_analysis_owner', 'pka_full_analysis_immutable', 'pka_full_analysis_domain_delete'):
        connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{name}"')
    connection.exec_driver_sql('DROP TABLE IF EXISTS domain_full_analysis_jobs')


def remove_domain_processing_schema(connection):
    remove_full_analysis_schema(connection)
    names = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger'").scalars().all()
    for name in names:
        if name.startswith(('pka_domain_', 'pka_change_domain_processing_', 'pka_topic_', 'pka_change_domain_topic', 'pka_node_', 'pka_build_', 'pka_change_domain_summary')):
            connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
    connection.exec_driver_sql('DROP TABLE IF EXISTS domain_processing_states')
    connection.exec_driver_sql('DROP TABLE IF EXISTS domain_processing_preferences')
    connection.exec_driver_sql('DROP TABLE IF EXISTS domain_topic_members')
    connection.exec_driver_sql('DROP TABLE IF EXISTS domain_topics')
    connection.exec_driver_sql('DROP TABLE IF EXISTS domain_topic_index_states')

    connection.exec_driver_sql('DROP TABLE IF EXISTS domain_node_embeddings')
    connection.exec_driver_sql('DROP TABLE IF EXISTS domain_node_index_jobs')
    connection.exec_driver_sql('DROP TABLE IF EXISTS domain_summary_jobs')
    connection.exec_driver_sql('DROP TABLE IF EXISTS domain_summary_artifacts')

    connection.exec_driver_sql('DROP TABLE IF EXISTS domain_builds')
