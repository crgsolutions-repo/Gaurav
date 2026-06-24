from supabase import create_client

from config import Config, require_config


require_config("SUPABASE_URL", "SUPABASE_KEY")

supabase = create_client(Config.SUPABASE_URL, Config.SUPABASE_KEY)
rag_supabase = (
    create_client(Config.SUPABASE_URL, Config.SUPABASE_SERVICE_ROLE_KEY)
    if Config.SUPABASE_SERVICE_ROLE_KEY
    else supabase
)
