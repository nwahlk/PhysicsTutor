CREATE TABLE students (
    id         text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
    thread_id  text PRIMARY KEY,
    student_id text NOT NULL REFERENCES students(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX sessions_student_idx ON sessions (student_id);
