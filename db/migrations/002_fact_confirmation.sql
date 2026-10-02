ALTER TABLE facts ADD COLUMN status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','confirmed'));
ALTER TABLE facts ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0);
ALTER TABLE facts ADD COLUMN origin TEXT NOT NULL DEFAULT 'legacy' CHECK (origin IN ('human','model','legacy'));
ALTER TABLE facts ADD COLUMN school TEXT NOT NULL DEFAULT '';
ALTER TABLE facts ADD COLUMN degree TEXT NOT NULL DEFAULT '';
ALTER TABLE facts ADD COLUMN major TEXT NOT NULL DEFAULT '';
CREATE TABLE fact_revisions (fact_id TEXT NOT NULL REFERENCES facts(id), revision INTEGER NOT NULL, snapshot JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(fact_id,revision));
CREATE TABLE profile (id INTEGER PRIMARY KEY CHECK (id=1), revision INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('draft','confirmed')), data JSONB NOT NULL);
CREATE TABLE profile_revisions (revision INTEGER PRIMARY KEY, snapshot JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
INSERT INTO fact_revisions(fact_id,revision,snapshot) SELECT id,revision,to_jsonb(facts)-'embedding'-'created_at'-'updated_at' FROM facts;
CREATE FUNCTION reject_revision_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'revision history is immutable'; END $$;
CREATE TRIGGER fact_history_immutable BEFORE UPDATE OR DELETE ON fact_revisions FOR EACH ROW EXECUTE FUNCTION reject_revision_mutation();
CREATE TRIGGER profile_history_immutable BEFORE UPDATE OR DELETE ON profile_revisions FOR EACH ROW EXECUTE FUNCTION reject_revision_mutation();

UPDATE facts SET embedding=NULL;
