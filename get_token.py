import os
import json
from google_auth_oauthlib.flow import InstalledAppFlow

# Define los alcances necesarios
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly', 'https://www.googleapis.com/auth/calendar']

def main():
    """
    Ejecuta este script localmente para generar el contenido del secreto GOOGLE_USER_CREDS.
    Requiere un archivo 'credentials.json' descargado de Google Cloud Console.
    """
    if not os.path.exists('credentials.json'):
        print("Error: No se encuentra 'credentials.json'. Descárgalo de GCP -> APIs & Services -> Credentials.")
        return

    flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
    # Usamos run_local_server con parámetros para facilitar el uso en entornos remotos
    # port=0 buscará un puerto libre. 
    print("\n1. Haz clic en el enlace de abajo para autorizar la aplicación.")
    print("2. Después de autorizar, tu navegador intentará ir a 'localhost:XXXX'.")
    print("3. SI TE DA ERROR DE CONEXIÓN, no te preocupes. Copia la URL completa de la barra de direcciones.")
    print("4. Esa URL contiene el código que necesitamos.")
    
    creds = flow.run_local_server(port=0, open_browser=False)

    # Convertir credenciales a formato JSON para el secreto de GitHub
    creds_data = {
        'token': creds.token,
        'refresh_token': creds.refresh_token,
        'token_uri': creds.token_uri,
        'client_id': creds.client_id,
        'client_secret': creds.client_secret,
        'scopes': creds.scopes
    }
    
    print("\n--- COPIA EL SIGUIENTE CONTENIDO Y PÉGALO EN EL SECRETO 'GOOGLE_USER_CREDS' DE GITHUB ---\n")
    print(json.dumps(creds_data, indent=2))
    print("\n------------------------------------------------------------------------------------------\n")

if __name__ == '__main__':
    main()
