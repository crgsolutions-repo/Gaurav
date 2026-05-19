from supabase_client import supabase

response = supabase.table("employees").select("*").execute()

print(response)