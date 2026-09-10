import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_ANON_KEY"])

response = client.auth.sign_in_with_password({
    "email": "abc@gmail.com",   # whatever email you registered
    "password": "abc@123",   # whatever password you set
})

print(response.session.access_token)