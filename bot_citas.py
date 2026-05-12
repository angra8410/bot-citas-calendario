import os
import datetime
import json
import base64
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google import genai

# Configuración de alcances
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly', 'https://www.googleapis.com/auth/calendar']

def extract_appointment_data(text, api_key):
    """Uses the new Google GenAI SDK to extract structured date and time."""
    if not api_key:
        print("GEMINI_API_KEY no proporcionada.")
        return None

    try:
        client = genai.Client(api_key=api_key)
        
        # --- DIAGNÓSTICO: Listar modelos disponibles ---
        try:
            print("DEBUG: Listando modelos disponibles para esta API Key...")
            for m in client.models.list():
                print(f"DEBUG: Modelo disponible: {m.name} | Versiones: {m.supported_methods}")
        except Exception as list_err:
            print(f"DEBUG: No se pudieron listar los modelos: {list_err}")
        # -----------------------------------------------

        prompt = f"""
        Analiza el siguiente texto de un correo y extrae la fecha y hora del PRÓXIMO o SIGUIENTE evento (cita, webinar, reunión, encuentro).
        
        IMPORTANTE: 
        1. Ignora fechas pasadas (como menciones de webinars que ya ocurrieron). 
        2. Busca frases como "Nuestra cita es", "próximo encuentro", "Fecha:", "Mayo 13 de 2026".
        3. Responde ÚNICAMENTE con un objeto JSON con las llaves "fecha" (YYYY-MM-DD) y "hora" (HH:MM en formato 24h).
        4. Si hay un rango de horas (ej. 6:00 pm a 7:00 pm), usa la hora de inicio (18:00).
        5. Si el texto no menciona ninguna cita futura clara, responde únicamente con {{}}.
        
        Hoy es {datetime.date.today().isoformat()}.
        
        Texto del correo:
        "{text[:5000]}"
        """
        
        # Intentamos con el nombre de modelo más estándar para el nuevo SDK
        response = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=prompt
        )
        
        text_response = response.text.strip()
        print(f"DEBUG: Respuesta de Gemini: {text_response}")
        
        if "{" in text_response:
            json_str = text_response[text_response.find("{"):text_response.rfind("}")+1]
            data = json.loads(json_str)
            if data and 'fecha' in data and 'hora' in data:
                return data
        return None
    except Exception as e:
        print(f"Error procesando con Gemini: {e}")
        return None

def main():
    creds_info = os.environ.get('GOOGLE_USER_CREDS')
    if not creds_info:
        print("Error: GOOGLE_USER_CREDS no configurado.")
        return
        
    try:
        creds_dict = json.loads(creds_info)
        creds = Credentials.from_authorized_user_info(info=creds_dict, scopes=SCOPES)
    except Exception as e:
        print(f"Error cargando credenciales: {e}")
        return

    gemini_api_key = os.environ.get('GEMINI_API_KEY')
    
    gmail = build('gmail', 'v1', credentials=creds)
    calendar = build('calendar', 'v3', credentials=creds)

    # Búsqueda extremadamente amplia para no perder nada
    query = '("cita" OR "citas" OR "Ruta N" OR "webinar" OR "encuentro") newer_than:7d'
    print(f"DEBUG: Buscando correos con query: {query}")
    
    try:
        results = gmail.users().messages().list(userId='me', q=query).execute()
        messages = results.get('messages', [])
        print(f"DEBUG: Se encontraron {len(messages)} mensajes potenciales.")
    except Exception as e:
        print(f"Error al listar mensajes de Gmail: {e}")
        return

    for msg_ref in messages:
        msg_id = msg_ref['id']
        try:
            msg = gmail.users().messages().get(userId='me', id=msg_id).execute()
            subject = "Sin Asunto"
            headers = msg.get('payload', {}).get('headers', [])
            for header in headers:
                if header['name'] == 'Subject':
                    subject = header['value']
            
            print(f"\n--- Analizando: {subject} (ID: {msg_id}) ---")
            
            def get_body(payload):
                """Recursively extracts the body, prioritizing plain text, then HTML."""
                parts = payload.get('parts', [])
                
                # If it's a simple message without parts
                if not parts:
                    return payload.get('body', {}).get('data')
                
                # Look for plain text first
                for part in parts:
                    if part.get('mimeType') == 'text/plain':
                        return part.get('body', {}).get('data')
                
                # Then look for HTML
                for part in parts:
                    if part.get('mimeType') == 'text/html':
                        return part.get('body', {}).get('data')
                
                # Recursively look in nested parts
                for part in parts:
                    data = get_body(part)
                    if data:
                        return data
                return None

            body_data = get_body(msg.get('payload', {}))
            if body_data:
                try:
                    # Usamos decode con error='ignore' por si hay caracteres extraños
                    body = base64.urlsafe_b64decode(body_data).decode('utf-8', errors='ignore')
                    print(f"DEBUG: Cuerpo extraído ({len(body)} caracteres).")
                    
                    appointment_data = extract_appointment_data(body, gemini_api_key)
                    
                    if appointment_data:
                        fecha = appointment_data.get('fecha')
                        hora = appointment_data.get('hora')
                        print(f"DEBUG: Datos extraídos: Fecha={fecha}, Hora={hora}")
                        
                        try:
                            fecha_cita = datetime.datetime.strptime(f"{fecha} {hora}", "%Y-%m-%d %H:%M")
                        except Exception as parse_err:
                            print(f"Error al parsear fecha/hora '{fecha} {hora}': {parse_err}")
                            continue
                        
                        if is_duplicate(calendar, fecha_cita, msg_id):
                            print(f"⚠️ Omitido: Ya existe una cita programada para este correo o a esta hora.")
                            continue

                        event = {
                            'summary': f'Evento: {subject[:50]}',
                            'description': f'Correo ID: {msg_id}\n\nSnippet: {msg["snippet"]}',
                            'start': {'dateTime': fecha_cita.isoformat(), 'timeZone': 'America/Bogota'},
                            'end': {'dateTime': (fecha_cita + datetime.timedelta(hours=1)).isoformat(), 'timeZone': 'America/Bogota'},
                            'reminders': {
                                'useDefault': False,
                                'overrides': [
                                    {'method': 'email', 'minutes': 24 * 60},
                                    {'method': 'email', 'minutes': 60},
                                    {'method': 'popup', 'minutes': 30},
                                ],
                            },
                        }
                        
                        calendar.events().insert(calendarId='primary', body=event).execute()
                        print(f"✅ ÉXITO: Cita agendada para {fecha} {hora}")
                    else:
                        print("DEBUG: Gemini no detectó una cita válida en el contenido.")
                except Exception as decode_err:
                    print(f"Error decodificando cuerpo: {decode_err}")
            else:
                print("DEBUG: No se pudo extraer el cuerpo del mensaje.")
        except Exception as e:
            print(f"Error con mensaje {msg_id}: {e}")
        except Exception as e:
            print(f"Error con mensaje {msg_id}: {e}")

if __name__ == '__main__':
    main()
