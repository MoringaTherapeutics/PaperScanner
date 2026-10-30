-- Global plant chemistry scanner: authoritative schema (PostgreSQL).
-- Every write from application code is an idempotent upsert on the unique keys below.
-- Original measurements are never overwritten: corrections go to `corrections`.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS species (
    species_id        text PRIMARY KEY,               -- e.g. SP_CURCUMA_LONGA
    accepted_name     text NOT NULL,
    authority         text,
    priority          integer NOT NULL DEFAULT 100,   -- lower runs first
    coverage_status   text NOT NULL DEFAULT 'Not started'
        CHECK (coverage_status IN (
            'Not started','Discovery in progress','Retrieval incomplete',
            'Screening in progress','Full-text extraction in progress',
            'Coverage audit pending','Complete within documented search scope',
            'Reopening due to newly discovered literature')),
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS synonyms (
    species_id  text NOT NULL REFERENCES species(species_id),
    name        text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('synonym','common_name','spelling','cultivar')),
    language    text,
    PRIMARY KEY (species_id, name)
);

CREATE TABLE IF NOT EXISTS search_runs (
    run_id            text PRIMARY KEY,               -- sha1(species|query|filter) + run timestamp
    species_id        text NOT NULL REFERENCES species(species_id),
    query_family      text NOT NULL,
    query_hash        text NOT NULL,
    database          text NOT NULL DEFAULT 'OpenAlex',
    query_string      text NOT NULL,
    filters           text NOT NULL DEFAULT '',
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    reported_total    bigint,
    retrieved_count   bigint NOT NULL DEFAULT 0,
    failed_count      bigint NOT NULL DEFAULT 0,
    cursor_checkpoint text,
    pages_done        integer NOT NULL DEFAULT 0,
    retry_count       integer NOT NULL DEFAULT 0,
    pagination_complete boolean NOT NULL DEFAULT false,
    errors            text,
    software_version  text,
    UNIQUE (species_id, query_hash, filters, started_at)
);

CREATE TABLE IF NOT EXISTS publications (
    publication_id    text PRIMARY KEY,               -- OpenAlex ID, e.g. W4407340576
    doi               text UNIQUE,                    -- lower-cased, no URL prefix
    pmid              text,
    pmcid             text,
    title             text NOT NULL,
    year              integer,
    venue             text,
    work_type         text,
    authors           jsonb,
    is_oa             boolean,
    oa_urls           jsonb,
    raw               jsonb,
    duplicate_of      text REFERENCES publications(publication_id),
    first_seen_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS search_results (
    run_id          text NOT NULL REFERENCES search_runs(run_id),
    publication_id  text NOT NULL REFERENCES publications(publication_id),
    rank            integer,
    PRIMARY KEY (run_id, publication_id)
);

CREATE TABLE IF NOT EXISTS species_publications (
    species_id      text NOT NULL REFERENCES species(species_id),
    publication_id  text NOT NULL REFERENCES publications(publication_id),
    PRIMARY KEY (species_id, publication_id)
);

CREATE TABLE IF NOT EXISTS screening (
    publication_id  text NOT NULL REFERENCES publications(publication_id),
    species_id      text NOT NULL REFERENCES species(species_id),
    stage           smallint NOT NULL CHECK (stage IN (1,2)),
    decision        text NOT NULL
        CHECK (decision IN ('likely_eligible','likely_ineligible','uncertain',
                            'include','exclude','requires_review')),
    reason          text,
    evidence        text,
    reviewer        text NOT NULL,                    -- model version or human id
    fulltext_available boolean,
    decided_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (publication_id, species_id, stage, reviewer)
);

CREATE TABLE IF NOT EXISTS source_documents (
    publication_id  text PRIMARY KEY REFERENCES publications(publication_id),
    status          text NOT NULL CHECK (status IN ('retrieved','unavailable','failed')),
    source_url      text,
    version_source  text,                             -- publisher | pmc | repository | preprint
    content_sha256  text,
    n_chars         integer,
    error           text,
    retrieved_at    timestamptz NOT NULL DEFAULT now()
);

-- Study = the experiment reported in a publication (may be several per publication).
CREATE TABLE IF NOT EXISTS studies (
    study_id        text PRIMARY KEY,                 -- workbook ID (BS001) or generated
    publication_id  text REFERENCES publications(publication_id),
    species_id      text REFERENCES species(species_id),
    claimed_title   text,                             -- as typed in the legacy workbook
    claimed_doi     text,
    verification_status text NOT NULL DEFAULT 'U' CHECK (verification_status IN ('Y','N','U')),
    verification_note   text,
    origin          text NOT NULL DEFAULT 'pipeline'  -- pipeline | legacy_workbook
);

CREATE TABLE IF NOT EXISTS extraction_protocols (
    protocol_id     text PRIMARY KEY,                 -- sha1 of the normalized protocol fields
    technique       text,
    solvent         text,
    solvent_composition text,
    solvent_concentration text,
    solid_liquid_ratio text,
    temperature     text,
    duration        text,
    cycles          text,
    pressure        text,
    sequential_order integer,
    notes           text
);

CREATE TABLE IF NOT EXISTS extracts (
    extract_id      text PRIMARY KEY,                 -- sha1(study|part|prep|protocol|condition)
    study_id        text NOT NULL REFERENCES studies(study_id),
    species_id      text REFERENCES species(species_id),
    plant_part      text,
    cultivar        text,
    origin_geo      text,
    sample_state    text,                             -- fresh | dried | other | unknown
    preparation     text,
    protocol_id     text REFERENCES extraction_protocols(protocol_id),
    condition       text,                             -- independent experimental condition
    extraction_yield text,
    biological_replicates text,
    technical_replicates  text,
    method_locator  text,
    flag_macronutrients  char(1) NOT NULL DEFAULT 'U' CHECK (flag_macronutrients IN ('Y','N','U')),
    flag_minerals        char(1) NOT NULL DEFAULT 'U' CHECK (flag_minerals IN ('Y','N','U')),
    flag_polyphenols     char(1) NOT NULL DEFAULT 'U' CHECK (flag_polyphenols IN ('Y','N','U')),
    flag_tannins         char(1) NOT NULL DEFAULT 'U' CHECK (flag_tannins IN ('Y','N','U')),
    flag_bioactives      char(1) NOT NULL DEFAULT 'U' CHECK (flag_bioactives IN ('Y','N','U')),
    flag_antioxidant_assays char(1) NOT NULL DEFAULT 'U' CHECK (flag_antioxidant_assays IN ('Y','N','U'))
);

CREATE TABLE IF NOT EXISTS analytes (
    analyte_id      text PRIMARY KEY,                 -- slug of normalized name
    name            text NOT NULL,
    category        text,
    chem_identifier text                              -- PubChem CID / InChIKey when known
);

CREATE TABLE IF NOT EXISTS measurements (
    measurement_id  text PRIMARY KEY,                 -- sha1(extract|analyte|basis|locator)
    extract_id      text NOT NULL REFERENCES extracts(extract_id),
    analyte_id      text NOT NULL REFERENCES analytes(analyte_id),
    value_original  text NOT NULL,                    -- exactly as printed
    value_numeric   double precision,
    unit_original   text,
    basis           text NOT NULL DEFAULT 'unknown'
        CHECK (basis IN ('fresh','dry','extract','other','unknown')),
    uncertainty_type  text CHECK (uncertainty_type IN ('SD','SE','CI','range','none','unknown')),
    uncertainty_value text,
    n               integer,
    method          text,
    calibration_standard text,
    lod             text,
    loq             text,
    locator         text NOT NULL,                    -- page / table / figure / supplement
    source_quote    text NOT NULL,
    value_normalized double precision,                -- separate from original; needs moisture support
    unit_normalized  text,
    verification_status text NOT NULL DEFAULT 'U' CHECK (verification_status IN ('Y','N','U')),
    verified_by     text,
    verified_at     timestamptz,
    model_version   text
);

CREATE TABLE IF NOT EXISTS quarantine (
    quarantine_id   bigserial PRIMARY KEY,
    kind            text NOT NULL,                    -- measurement | study | legacy_value | legacy_robustness
    ref_id          text,                             -- study_id / publication_id
    payload         jsonb NOT NULL,
    reasons         text[] NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, ref_id, payload)
);

CREATE TABLE IF NOT EXISTS quality_assessments (
    study_id    text NOT NULL REFERENCES studies(study_id),
    domain      text NOT NULL,
    rating      text NOT NULL CHECK (rating IN
        ('low concern','some concerns','high concern','insufficient information')),
    evidence    text,
    PRIMARY KEY (study_id, domain)
);

CREATE TABLE IF NOT EXISTS validation_reviews (
    measurement_id text NOT NULL REFERENCES measurements(measurement_id),
    reviewer       text NOT NULL,
    verdict        text NOT NULL CHECK (verdict IN ('approve','reject','needs_work')),
    note           text,
    reviewed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (measurement_id, reviewer)
);

CREATE TABLE IF NOT EXISTS corrections (
    correction_id bigserial PRIMARY KEY,
    table_name    text NOT NULL,
    row_id        text NOT NULL,
    field         text NOT NULL,
    old_value     text,
    new_value     text,
    reason        text NOT NULL,
    corrected_by  text NOT NULL,
    corrected_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS coverage_audits (
    audit_id      bigserial PRIMARY KEY,
    species_id    text NOT NULL REFERENCES species(species_id),
    audited_at    timestamptz NOT NULL DEFAULT now(),
    status        text NOT NULL,
    checks        jsonb NOT NULL,                     -- the 11 audit questions, each true/false/null
    sentinel_recall double precision,
    notes         text
);

CREATE TABLE IF NOT EXISTS execution_log (
    exec_id       bigserial PRIMARY KEY,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    report        jsonb,
    software_version text,
    errors        text
);

CREATE TABLE IF NOT EXISTS task_queue (
    task_id      bigserial PRIMARY KEY,
    species_id   text NOT NULL REFERENCES species(species_id),
    task_type    text NOT NULL,                       -- search | screen | fulltext | extract
    ref_id       text NOT NULL,                       -- query_hash or publication_id
    state        text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','running','done','failed')),
    attempts     integer NOT NULL DEFAULT 0,
    last_error   text,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (species_id, task_type, ref_id)
);

CREATE INDEX IF NOT EXISTS idx_pub_year ON publications(year);
CREATE INDEX IF NOT EXISTS idx_meas_extract ON measurements(extract_id);
CREATE INDEX IF NOT EXISTS idx_meas_status ON measurements(verification_status);
CREATE INDEX IF NOT EXISTS idx_queue_state ON task_queue(state, task_type);

INSERT INTO schema_migrations(version) VALUES ('001') ON CONFLICT DO NOTHING;
