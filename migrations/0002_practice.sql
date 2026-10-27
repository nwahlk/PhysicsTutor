CREATE TABLE question_templates (
    id          text NOT NULL,
    version     integer NOT NULL CHECK (version > 0),
    kp_ids      text[] NOT NULL CHECK (cardinality(kp_ids) > 0),
    difficulty smallint NOT NULL CHECK (difficulty BETWEEN 1 AND 5),
    type        text NOT NULL CHECK (type IN ('numeric', 'multi_step_numeric', 'choice')),
    spec        jsonb NOT NULL,
    spec_sha256 text NOT NULL CHECK (spec_sha256 ~ '^[0-9a-f]{64}$'),
    verified    boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, version)
);

CREATE TABLE questions (
    id               text PRIMARY KEY,
    template_id      text,
    template_version integer,
    kp_ids           text[] NOT NULL CHECK (cardinality(kp_ids) > 0),
    type             text NOT NULL CHECK (type IN ('numeric', 'multi_step_numeric', 'choice')),
    difficulty       smallint NOT NULL CHECK (difficulty BETWEEN 1 AND 5),
    stem             text NOT NULL,
    params           jsonb NOT NULL DEFAULT '{}'::jsonb,
    answer_spec      jsonb NOT NULL,
    solution_spec    jsonb NOT NULL,
    rubric           jsonb NOT NULL,
    symbols          jsonb NOT NULL DEFAULT '{}'::jsonb,
    source           text NOT NULL CHECK (source IN ('bank', 'template')),
    source_url       text,
    content_sha256   text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    seed             bigint CHECK (seed >= 0),
    verified         boolean NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT questions_template_fk
        FOREIGN KEY (template_id, template_version)
        REFERENCES question_templates (id, version),
    CONSTRAINT questions_template_shape CHECK (
        (source = 'template' AND template_id IS NOT NULL AND template_version IS NOT NULL AND seed IS NOT NULL)
        OR
        (source = 'bank' AND template_id IS NULL AND template_version IS NULL AND seed IS NULL AND source_url IS NOT NULL)
    )
);

CREATE TABLE attempts (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    student_id  text NOT NULL REFERENCES students(id),
    question_id text NOT NULL REFERENCES questions(id),
    thread_id   text NOT NULL REFERENCES sessions(thread_id),
    attempt_no  smallint NOT NULL CHECK (attempt_no BETWEEN 1 AND 2),
    answer      jsonb NOT NULL,
    grade       jsonb NOT NULL,
    correct     boolean NOT NULL,
    score       smallint NOT NULL CHECK (score BETWEEN 0 AND 100),
    error_types text[] NOT NULL DEFAULT '{}',
    graded_by   text NOT NULL DEFAULT 'rule' CHECK (graded_by = 'rule'),
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (thread_id, question_id, attempt_no)
);

CREATE INDEX questions_kp_ids_idx ON questions USING GIN (kp_ids);
CREATE UNIQUE INDEX questions_content_sha_idx ON questions (content_sha256) WHERE source = 'bank';
CREATE INDEX attempts_student_created_idx ON attempts (student_id, created_at DESC);
CREATE INDEX attempts_student_question_idx ON attempts (student_id, question_id);
