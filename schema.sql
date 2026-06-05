-- ClassQ PostgreSQL schema
-- Source of truth: .kiro/specs/classq-course-registration/design.md
--
-- NOTE ON ORDERING: course_sections has a foreign key to registration_windows,
-- so registration_windows is created before course_sections here. The table
-- definitions themselves are copied verbatim from design.md.

-- Students -------------------------------------------------------------
CREATE TABLE students (
    student_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id     TEXT NOT NULL UNIQUE,          -- university id
    display_name    TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Courses --------------------------------------------------------------
CREATE TABLE courses (
    course_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    course_code     TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Prerequisite DAG edges: prereq_course_id is a prerequisite of course_id
CREATE TABLE prerequisites (
    course_id           UUID NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    prereq_course_id    UUID NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    PRIMARY KEY (course_id, prereq_course_id),
    CONSTRAINT no_self_prereq CHECK (course_id <> prereq_course_id)
);
CREATE INDEX idx_prereq_by_course  ON prerequisites(course_id);
CREATE INDEX idx_prereq_by_prereq  ON prerequisites(prereq_course_id);  -- transitive invalidation

-- Registration windows -------------------------------------------------
CREATE TABLE registration_windows (
    window_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    opens_at        TIMESTAMPTZ NOT NULL,
    closes_at       TIMESTAMPTZ NOT NULL,
    drain_seconds   INTEGER NOT NULL DEFAULT 30 CHECK (drain_seconds >= 0),
    max_queue       INTEGER NOT NULL CHECK (max_queue >= 0),
    state           TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK (state IN ('scheduled','open','draining','closed')),
    CONSTRAINT window_time_order CHECK (closes_at > opens_at)
);

-- Course sections (capacity lives here) --------------------------------
CREATE TABLE course_sections (
    section_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    course_id           UUID NOT NULL REFERENCES courses(course_id),
    window_id           UUID NOT NULL REFERENCES registration_windows(window_id),
    section_label       TEXT NOT NULL,
    capacity            INTEGER NOT NULL CHECK (capacity >= 0),
    confirmed_count     INTEGER NOT NULL DEFAULT 0 CHECK (confirmed_count >= 0),
    -- Durable no-overselling invariant backstop (R1.3, R9):
    CONSTRAINT no_oversell CHECK (confirmed_count <= capacity),
    UNIQUE (course_id, section_label, window_id)
);
CREATE INDEX idx_sections_by_window ON course_sections(window_id);

-- Enrollments ----------------------------------------------------------
CREATE TABLE enrollments (
    enrollment_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id      UUID NOT NULL REFERENCES students(student_id),
    section_id      UUID NOT NULL REFERENCES course_sections(section_id),
    status          TEXT NOT NULL DEFAULT 'confirmed'
                    -- 'waitlisted' supports Section Waitlisting & Auto-Promotion (R12):
                    CHECK (status IN ('confirmed','released','completed','waitlisted')),
    is_simulated    BOOLEAN NOT NULL DEFAULT false,   -- chaos tagging (R8.5)
    confirmed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One confirmed seat per student per section:
    CONSTRAINT uq_confirmed_enrollment UNIQUE (student_id, section_id)
);
CREATE INDEX idx_enroll_by_section ON enrollments(section_id) WHERE status = 'confirmed';
-- Completed enrollments back prerequisite satisfaction (R4.2)
CREATE INDEX idx_enroll_completed  ON enrollments(student_id) WHERE status = 'completed';

-- Transactional Outbox -------------------------------------------------
CREATE TABLE outbox (
    event_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),  -- stable dedup id (R6.5)
    section_id      UUID NOT NULL REFERENCES course_sections(section_id),
    sequence        BIGINT GENERATED ALWAYS AS IDENTITY,         -- per-insert monotonic ordering (R6.6)
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','processed','failed')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at    TIMESTAMPTZ
);
-- Processor scans pending rows ordered per section by sequence (R6.2, R6.6):
CREATE INDEX idx_outbox_pending ON outbox(section_id, sequence) WHERE status = 'pending';

-- Data retention / pruning (R6): a scheduled DB job (pg_cron) runs daily to
-- delete long-processed rows so the table cannot grow without bound.
-- SELECT cron.schedule('outbox-prune', '0 3 * * *', $$
--   DELETE FROM outbox
--   WHERE status = 'processed' AND processed_at < NOW() - INTERVAL '7 days';
-- $$);
