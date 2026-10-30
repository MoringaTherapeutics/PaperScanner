-- Staging for rows imported from the legacy workbook. These are NOT verified data.
-- Citation confirmed in OpenAlex != value verified: each value stays 'U' until a
-- person or the quote-check pipeline finds it in the full text.

CREATE TABLE IF NOT EXISTS legacy_values (
    study_id          text NOT NULL REFERENCES studies(study_id),
    row_no            integer NOT NULL,
    plant_part_or_state text,
    endpoint          text,
    value_text        text,
    unit_text         text,
    value_type        text,
    basis_text        text,
    method            text,
    source_note       text,
    verification_status text NOT NULL DEFAULT 'U' CHECK (verification_status IN ('Y','N','U')),
    PRIMARY KEY (study_id, row_no)
);

CREATE TABLE IF NOT EXISTS legacy_robustness (
    study_id    text NOT NULL REFERENCES studies(study_id),
    domain      text NOT NULL,
    score_0_3   integer,
    evidence    text,
    caution     text,
    PRIMARY KEY (study_id, domain)
);

ALTER TABLE studies ADD COLUMN IF NOT EXISTS citation_status text;   -- confirmed | unmatched_wrong_doi | unmatched_not_found
ALTER TABLE studies ADD COLUMN IF NOT EXISTS openalex_id text;

INSERT INTO schema_migrations(version) VALUES ('002') ON CONFLICT DO NOTHING;
