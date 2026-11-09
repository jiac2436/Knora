"""Version 10 topic constraints and manual-input invalidation triggers."""
from .domain_changes import _mark


def expected_topic_triggers() -> dict[str, str]:
    result = {}
    for event in ('INSERT', 'UPDATE'):
        name = 'pka_topic_parent_' + event.lower()
        result[name] = f"""CREATE TRIGGER {name} BEFORE {event} ON domain_topics
            WHEN NOT EXISTS (SELECT 1 FROM domains WHERE id=NEW.domain_id AND owner_id=NEW.owner_id)
            OR (NEW.parent_id IS NOT NULL AND (NEW.parent_id=NEW.id
                OR NOT EXISTS (SELECT 1 FROM domain_topics p WHERE p.id=NEW.parent_id
                    AND p.domain_id=NEW.domain_id AND p.owner_id=NEW.owner_id
                    AND p.scope=NEW.scope AND p.parent_id IS NULL)
                OR EXISTS (SELECT 1 FROM domain_topics c WHERE c.parent_id=NEW.id)))
            BEGIN SELECT RAISE(ABORT, '主题父级或知识域不合法'); END"""
        name = 'pka_topic_member_' + event.lower()
        result[name] = f"""CREATE TRIGGER {name} BEFORE {event} ON domain_topic_members
            WHEN NOT EXISTS (SELECT 1 FROM domain_topics t JOIN sources s
                ON s.domain_id=t.domain_id AND s.owner_id=t.owner_id
                WHERE t.id=NEW.topic_id AND s.id=NEW.source_id)
            BEGIN SELECT RAISE(ABORT, '主题成员必须属于相同知识域'); END"""
    for table in ('domain_topics', 'domain_topic_members'):
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            row = 'OLD' if event == 'DELETE' else 'NEW'
            if table == 'domain_topics':
                domain, owner, scope = f'{row}.domain_id', f'{row}.owner_id', f'{row}.scope'
                gate = f'{row}.automatic_key IS NULL OR {row}.name_fixed = 1 OR {row}.parent_id IS NOT NULL'
                if event == 'UPDATE':
                    gate = "OLD.parent_id IS NOT NEW.parent_id OR OLD.name_fixed IS NOT NEW.name_fixed OR (NEW.name_fixed=1 AND OLD.name IS NOT NEW.name)"
            else:
                domain = f'(SELECT domain_id FROM domain_topics WHERE id={row}.topic_id)'
                owner = f'(SELECT owner_id FROM domain_topics WHERE id={row}.topic_id)'
                scope = f'(SELECT scope FROM domain_topics WHERE id={row}.topic_id)'
                gate = f'{row}.manual = 1'
                if event == 'UPDATE':
                    gate = '(OLD.manual=1 OR NEW.manual=1) AND (OLD.included IS NOT NEW.included OR OLD.manual IS NOT NEW.manual OR OLD.topic_id IS NOT NEW.topic_id OR OLD.source_id IS NOT NEW.source_id)'
            name = f'pka_topic_dirty_{table}_{event.lower()}'
            body = _mark(domain, owner, f"scopes.scope = {scope}")
            result[name] = f'CREATE TRIGGER {name} AFTER {event} ON {table} WHEN ({gate}) BEGIN {body} END'
    result['pka_topic_delete'] = """CREATE TRIGGER pka_topic_delete BEFORE DELETE ON domain_topics BEGIN
        DELETE FROM domain_topic_members WHERE topic_id=OLD.id;
        UPDATE domain_topics SET parent_id=NULL WHERE parent_id=OLD.id; END"""
    result['pka_topic_source_delete'] = """CREATE TRIGGER pka_topic_source_delete BEFORE DELETE ON sources BEGIN
        DELETE FROM domain_topic_members WHERE source_id=OLD.id; END"""
    result['pka_topic_source_move'] = """CREATE TRIGGER pka_topic_source_move BEFORE UPDATE ON sources
        WHEN OLD.domain_id IS NOT NEW.domain_id OR OLD.owner_id IS NOT NEW.owner_id
        BEGIN DELETE FROM domain_topic_members WHERE source_id=OLD.id; END"""
    result['pka_topic_domain_delete'] = """CREATE TRIGGER pka_topic_domain_delete BEFORE DELETE ON domains BEGIN
        DELETE FROM domain_topic_members WHERE topic_id IN (SELECT id FROM domain_topics WHERE domain_id=OLD.id);
        DELETE FROM domain_topics WHERE domain_id=OLD.id; END"""
    result['pka_topic_domain_cleanup'] = """CREATE TRIGGER pka_topic_domain_cleanup AFTER DELETE ON domains BEGIN
        DELETE FROM domain_topic_index_states WHERE domain_id=OLD.id;
        DELETE FROM domain_processing_states WHERE domain_id=OLD.id; END"""
    result['pka_topic_identity'] = """CREATE TRIGGER pka_topic_identity BEFORE UPDATE ON domain_topics
        WHEN OLD.id IS NOT NEW.id OR OLD.domain_id IS NOT NEW.domain_id OR OLD.owner_id IS NOT NEW.owner_id OR OLD.scope IS NOT NEW.scope
        BEGIN SELECT RAISE(ABORT, '主题身份不可变更'); END"""
    result['pka_topic_member_identity'] = """CREATE TRIGGER pka_topic_member_identity BEFORE UPDATE ON domain_topic_members
        WHEN OLD.topic_id IS NOT NEW.topic_id OR OLD.source_id IS NOT NEW.source_id
        BEGIN SELECT RAISE(ABORT, '成员身份不可变更'); END"""
    return result
