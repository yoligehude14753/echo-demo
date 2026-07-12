-- Durable, multi-process admission ledger for public identity enrollment.
CREATE TABLE public_enrollment_admissions (
    admission_id INTEGER PRIMARY KEY AUTOINCREMENT,
    enrollment_id_hash TEXT NOT NULL,
    peer_key_hash TEXT NOT NULL,
    admitted_at REAL NOT NULL
);

CREATE INDEX idx_public_enrollment_admissions_peer_time
    ON public_enrollment_admissions(peer_key_hash, admitted_at, admission_id);

CREATE INDEX idx_public_enrollment_admissions_global_time
    ON public_enrollment_admissions(admitted_at, admission_id);
