from supabase import create_client

from config import Config, require_config


require_config("SUPABASE_URL", "SUPABASE_KEY")

supabase = create_client(Config.SUPABASE_URL, Config.SUPABASE_KEY)
