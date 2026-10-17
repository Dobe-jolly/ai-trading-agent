"""Application logs for the agents: what no database records (an exception, a lost connection pool, a downstream that
refused). A deterministic pipeline, not a model reading log lines:

    profile (per service)  ->  parse (records, stack traces attached)  ->  mask (keyed hashes for identifiers)
        ->  signatures (the same error counted, per minute)  ->  store  ->  log_errors / log_search tools

Raw logs are read once at ingestion and never stored; what is stored is masked. Identifiers (accounts, mobiles, long
references) become keyed hashes, so a ticket's account can be searched for without any account number being kept.
"""
