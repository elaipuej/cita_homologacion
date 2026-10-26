"""
Nota de evaluación:
- Cómo lo hace: usa Playwright con una lógica de reintentos y memoria de horas bloqueadas para evitar repetir horas que ya fallaron o desaparecieron.
- Similitudes: sigue el mismo patrón base que las demás: abrir navegador, buscar disponibilidad, notificar y completar el trámite.
- Valoración: es más robusta que la versión básica y mejora el control de fallos, aunque sigue siendo bastante procedural.
"""

from playwright.sync_api import sync_playwright
import time
import winsound
from twilio.rest import Client
from datetime import datetime
import pyttsx3
import threading
import os
import json
import logging
import random

# ============================================================================
# CONFIGURACIÓN CENTRALIZADA
# ============================================================================
CONFIG = {
    "url": "https://citaprevia.ciencia.gob.es/qmaticwebbooking/#/",
    "sucursal": "Oficina asistencia telefónica",
    "servicio": "Asistencia reconocimiento títulos",
    "tiempo_bloqueo_segundos": 25,
    "max_reintentos_click": 1,
    "max_intentos_globales": 10000,
    "timeout_horas": 6,
    "espera_estabilidad_ms": 300,
    "espera_entre_busquedas_s": 0.8,
    "duracion_insistencia_s": 45,
    "intervalo_insistencia_ms": 500,
    "timeout_verificacion_ms": 500,
    "navegador": "chromium",  # Cambiado a chromium por compatibilidad reCAPTCHA e indetectabilidad
    "idioma": "es",  # Configurar navegador en español
}

# ============================================================================
# CONFIGURACIÓN DE LOGGING
# ============================================================================
# Crear log file handler con flush automático
log_file_handler = logging.FileHandler('cita_buscador.log', encoding='utf-8')
log_file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

logging.basicConfig(
    level=logging.DEBUG,
    handlers=[log_file_handler, console_handler]
)
logger = logging.getLogger(__name__)

def flush_logs():
    """Asegura que todos los logs se escriban al archivo"""
    for handler in logging.root.handlers:
        if isinstance(handler, logging.FileHandler):
            handler.flush()

# ============================================================================
# MEMORIA DE HORAS BLOQUEADAS
# Formato: {"HH:MM": timestamp_cuando_se_bloqueó}
# ============================================================================
horas_bloqueadas = {}

# ============================================================================
# REGISTRO DE INTENTOS Y FRACASOS
# ============================================================================
horas_intentadas_fallidas = []  # Lista de horas que fallaron en el intento actual

# ============================================================================
# CONFIGURACIÓN DE TWILIO
# ============================================================================
ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
NUMERO_TWILIO = os.getenv("TWILIO_WHATSAPP_FROM")
NUMERO_USUARIO = os.getenv("TWILIO_WHATSAPP_TO")

configuracion_twilio_faltante = [
    nombre
    for nombre, valor in (
        ("TWILIO_ACCOUNT_SID", ACCOUNT_SID),
        ("TWILIO_AUTH_TOKEN", AUTH_TOKEN),
        ("TWILIO_WHATSAPP_FROM", NUMERO_TWILIO),
        ("TWILIO_WHATSAPP_TO", NUMERO_USUARIO),
    )
    if not valor
]
if configuracion_twilio_faltante:
    raise RuntimeError(
        "Configura las variables de entorno requeridas para Twilio: "
        + ", ".join(configuracion_twilio_faltante)
    )

client = Client(ACCOUNT_SID, AUTH_TOKEN)

# ============================================================================
# ARCHIVO DE ESTADÍSTICAS
# ============================================================================
ARCHIVO_ESTADISTICAS = "estadisticas_citas.json"

def cargar_estadisticas():
    """Carga las estadísticas anteriores del archivo JSON"""
    try:
        if os.path.exists(ARCHIVO_ESTADISTICAS):
            with open(ARCHIVO_ESTADISTICAS, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error cargando estadísticas: {e}")
    
    # Retornar estructura inicial
    return {
        "total_citas_encontradas": 0,
        "historial": []
    }


def guardar_estadisticas(stats):
    """Guarda las estadísticas en archivo JSON"""
    try:
        with open(ARCHIVO_ESTADISTICAS, 'w', encoding='utf-8') as f:
            json.dump(stats, f, indent=2, ensure_ascii=False, default=str)
        logger.info(f"Estadísticas guardadas en {ARCHIVO_ESTADISTICAS}")
    except Exception as e:
        logger.error(f"Error guardando estadísticas: {e}")


def registrar_cita_encontrada(hora, num_horas, intentos, horas_fallidas=None):
    """Registra una cita encontrada en las estadísticas"""
    stats = cargar_estadisticas()
    
    cita_registro = {
        "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "hora": hora,
        "horas_disponibles": num_horas,
        "horas_intentadas_fallidas": horas_fallidas if horas_fallidas else [],
        "total_intentos": intentos,
        "sucursal": CONFIG["sucursal"],
        "servicio": CONFIG["servicio"]
    }
    
    stats["total_citas_encontradas"] += 1
    stats["historial"].append(cita_registro)
    
    guardar_estadisticas(stats)
    
    logger.info(f"✓ Cita #{stats['total_citas_encontradas']} registrada en estadísticas")
    return stats


# ============================================================================
# FUNCIONES DE NOTIFICACIÓN
# ============================================================================

def hablar(texto):
    """Reproduce texto con voz en un thread separado"""
    def speak():
        try:
            engine = pyttsx3.init()
            engine.setProperty('rate', 150)
            engine.say(texto)
            engine.runAndWait()
        except Exception as e:
            logger.debug(f"Error en síntesis de voz: {e}")
    
    thread = threading.Thread(target=speak, daemon=True)
    thread.start()
    return thread  # Retorna referencia por si se necesita


def enviar_whatsapp(mensaje):
    """Envía notificación por WhatsApp usando Twilio"""
    try:
        logger.info("Enviando WhatsApp...")
        message = client.messages.create(
            from_=NUMERO_TWILIO,
            body=mensaje,
            to=NUMERO_USUARIO
        )
        logger.info(f"WhatsApp enviado - SID: {message.sid}")
        return True
    except Exception as e:
        logger.error(f"Error enviando WhatsApp: {str(e)}")
        return False


# ============================================================================
# FUNCIONES DE GESTIÓN DE MEMORIA
# ============================================================================

def limpiar_horas_expiradas():
    """Elimina horas de la memoria si han pasado más de X segundos"""
    global horas_bloqueadas
    tiempo_actual = time.time()
    tiempo_bloqueo = CONFIG["tiempo_bloqueo_segundos"]
    
    horas_a_eliminar = []
    for hora, timestamp_bloqueo in horas_bloqueadas.items():
        tiempo_transcurrido = tiempo_actual - timestamp_bloqueo
        if tiempo_transcurrido > tiempo_bloqueo:
            horas_a_eliminar.append(hora)
    
    for hora in horas_a_eliminar:
        del horas_bloqueadas[hora]
        logger.info(f"Hora {hora} olvidada de la memoria (pasaron {tiempo_bloqueo}s)")
    
    return len(horas_bloqueadas) > 0


def bloquear_hora(hora):
    """Bloquea una hora en memoria porque no está disponible"""
    global horas_bloqueadas
    tiempo_bloqueo = CONFIG["tiempo_bloqueo_segundos"]
    horas_bloqueadas[hora] = time.time()
    logger.warning(f"Hora {hora} bloqueada en memoria (no volverá a intentarse en {tiempo_bloqueo}s)")


# ============================================================================
# FUNCIONES DE DETECCIÓN Y VERIFICACIÓN
# ============================================================================

def obtener_horas_disponibles_filtradas(page):
    """Obtiene horas disponibles excluyendo las bloqueadas en memoria"""
    try:
        # Limpiar horas expiradas primero
        limpiar_horas_expiradas()
        
        # Usar selector específico: botones con clase 'timeslot' que NO están disabled
        botones = page.locator("button.timeslot[aria-pressed='false']").all()
        
        horas = []
        for btn in botones:
            try:
                # Obtener valor del atributo en lugar de parsear texto
                hora = btn.get_attribute("value")
                if hora and ":" in hora:
                    # FILTRO: Excluir horas bloqueadas
                    if hora not in horas_bloqueadas:
                        horas.append(hora)
                    else:
                        tiempo_bloq = int(time.time() - horas_bloqueadas[hora])
                        logger.debug(f"Saltando hora {hora} (bloqueada hace {tiempo_bloq}s)")
            except Exception as e:
                logger.debug(f"Error procesando botón de hora: {e}")
        
        logger.debug(f"Horas disponibles (filtradas): {horas}")
        if horas_bloqueadas:
            logger.info(f"Horas en memoria: {list(horas_bloqueadas.keys())}")
        return horas
    except Exception as e:
        logger.error(f"Error obteniendo horas: {e}")
        return []


def verificar_disponibilidad_hora(page, hora):
    """Verifica si una hora específica sigue siendo disponible"""
    try:
        boton = page.locator(f"button.timeslot[value='{hora}'][aria-pressed='false']").first
        return boton.is_visible(timeout=CONFIG["timeout_verificacion_ms"])
    except Exception as e:
        logger.debug(f"Error verificando disponibilidad de {hora}: {e}")
        return False


def detectar_error_reserva(page):
    """Detecta si apareció el mensaje de error de reserva"""
    try:
        detectores = [
            page.locator("[role='alert']:has-text('reservad')"),
            page.locator("text=ya se ha reservado"),
        ]
        
        for detector in detectores:
            try:
                if detector.first.is_visible(timeout=600):
                    logger.warning("Error detectado: Hora ya reservada")
                    return True
            except Exception as e:
                logger.debug(f"Detector no encontró error: {e}")
        
        return False
    except Exception as e:
        logger.debug(f"Error en detección de reserva: {e}")
        return False


def detectar_recaptcha(page):
    """Detecta si hay un reCAPTCHA en la página"""
    try:
        # Búsqueda de elementos reCAPTCHA
        selectores = [
            "iframe[src*='recaptcha']",
            ".grecaptcha-badge",
            "[data-callback*='recaptcha']",
            "#recaptcha",
        ]
        
        for selector in selectores:
            try:
                elementos = page.locator(selector).all()
                if elementos:
                    logger.warning(f"⚠️  reCAPTCHA DETECTADO: {selector}")
                    return True
            except:
                pass
        
        return False
    except Exception as e:
        logger.debug(f"Error detectando reCAPTCHA: {e}")
        return False


def validar_campos_formulario(page):
    """Valida que los campos del formulario estén completados"""
    try:
        campos_requeridos = [
            ("FirstName", "Nombre"),
            ("LastName", "Apellido"),
            ("Email", "Email"),
            ("phone", "Teléfono"),
            ("Numero_expediente", "Número de expediente"),
        ]
        
        campos_vacios = []
        campos_completos = []
        
        for id_campo, nombre_campo in campos_requeridos:
            try:
                # Usar timeout reducido para ser más rápido, soportando ID o atributo name
                input_elem = page.locator(f"#{id_campo}, [name='{id_campo}']").first
                
                # Verificar si el elemento existe
                try:
                    input_elem.wait_for(state="visible", timeout=3000)
                except:
                    logger.debug(f"  ? {nombre_campo}: Campo no encontrado o no visible")
                    continue
                
                # Obtener el valor
                try:
                    valor = input_elem.input_value(timeout=3000)
                    
                    if valor and valor.strip():
                        campos_completos.append(nombre_campo)
                        logger.debug(f"  ✓ {nombre_campo}: {valor[:30]}...")
                    else:
                        campos_vacios.append(nombre_campo)
                        logger.info(f"  ✗ {nombre_campo}: VACÍO")
                except Exception as e:
                    logger.debug(f"  ? {nombre_campo}: No se pudo leer valor ({e})")
                    # Asumir que no está completado
                    campos_vacios.append(nombre_campo)
                    
            except Exception as e:
                logger.debug(f"  ? {nombre_campo}: Error general ({str(e)[:50]}...)")
                # Si hay error, asumir campo vacío
                campos_vacios.append(nombre_campo)
        
        return campos_vacios, campos_completos
    except Exception as e:
        logger.error(f"Error validando campos: {e}")
        # Retornar listas vacías para no bloquear
        return [], []


def esperar_confirmacion_recaptcha(page, duracion_max_segundos=600):
    """Espera a que el usuario resuelva el reCAPTCHA manualmente
    
    El timeout es de 600 segundos (10 minutos) para dar tiempo al usuario.
    El usuario puede presionar Ctrl+C en la terminal cuando termine de resolver el reCAPTCHA.
    """
    logger.info("\n" + "="*80)
    logger.info("⚠️  ATENCIÓN: reCAPTCHA DETECTADO")
    logger.info("="*80)
    logger.info("\n📝 INSTRUCCIONES:")
    logger.info("1. Se requiere completar el reCAPTCHA manualmente")
    logger.info("2. Abre el navegador (Chrome)")
    logger.info("3. Si no ves el formulario, haz scroll hacia abajo")
    logger.info("4. Verifica el reCAPTCHA en la esquina inferior derecha o en el formulario")
    logger.info("5. 🔑 IMPORTANTE: Presiona Ctrl+C AQUÍ cuando hayas completado el reCAPTCHA")
    logger.info(f"6. Si no presionas Ctrl+C, esperaremos máximo {duracion_max_segundos} segundos\n")
    
    tiempo_inicio = time.time()
    contador = 0
    
    try:
        while time.time() - tiempo_inicio < duracion_max_segundos:
            contador += 1
            
            # Mostrar progreso cada 10 segundos
            if contador % 5 == 0:
                segundos_transcurridos = int(time.time() - tiempo_inicio)
                logger.debug(f"Esperando reCAPTCHA... ({segundos_transcurridos}s de {duracion_max_segundos}s)")
            
            # Monitorear si el reCAPTCHA fue resuelto
            try:
                # Si desaparece el iframe del reCAPTCHA, probablemente fue resuelto
                iframe = page.locator("iframe[src*='recaptcha']").first
                if not iframe.is_visible(timeout=500):
                    logger.info("✅ reCAPTCHA probablemente fue resuelto (iframe desapareció)")
                    return True
            except:
                pass
            
            # También verificar si el checkbox de "No soy un robot" aparece marcado
            try:
                checkbox = page.locator("[aria-label*='reCAPTCHA']").first
                if checkbox.is_checked(timeout=500):
                    logger.info("✅ reCAPTCHA parece estar resuelto (checkbox marcado)")
                    return True
            except:
                pass
            
            time.sleep(2)
    
    except KeyboardInterrupt:
        logger.info("\n✓ Usuario presionó Ctrl+C - Continuando con el formulario")
        return True
    except Exception as e:
        logger.warning(f"Error esperando reCAPTCHA: {e}")
        # Continuar de todas formas, el usuario puede resolver manualmente
        return True
    
    logger.warning(f"⏱️  Timeout de {duracion_max_segundos} segundos esperando reCAPTCHA")
    logger.warning("⚠️  Continuando de todas formas... (es posible que ya esté resuelto)")
    # Continuar de todas formas
    return True


# ============================================================================
# FUNCIONES DE INTERACCIÓN
# ============================================================================

def hacer_click_con_reintentos(page, hora, max_intentos=None):
    """
    Intenta hacer click en una hora con reintentos y backoff exponencial
    Más flexible con verificaciones para detectar el éxito
    
    Retorna: (éxito, mensaje)
    """
    if max_intentos is None:
        max_intentos = CONFIG["max_reintentos_click"]
    
    logger.info(f"Iniciando click en hora {hora} (máx {max_intentos} intentos)")
    
    for intento in range(max_intentos):
        try:
            # Verificar disponibilidad ANTES de click
            if not verificar_disponibilidad_hora(page, hora):
                logger.warning(f"Intento {intento + 1}: Hora {hora} NO disponible antes de click")
                if intento < max_intentos - 1:
                    espera = (2 ** intento) + random.uniform(0, 0.5)
                    logger.info(f"Esperando {espera:.1f}s antes de reintentar...")
                    time.sleep(espera)
                continue
            
            # Hacer click
            logger.info(f"Intento {intento + 1}: Haciendo click en {hora}")
            boton = page.locator(f"button.timeslot[value='{hora}']").first
            
            boton.click(force=True, timeout=5000)
            logger.debug(f"  Click ejecutado, esperando respuesta...")
            
            # Esperar a que se procese el click
            time.sleep(2.0)
            
            # VERIFICACIÓN CRÍTICA: Detectar error de reserva (ÚNICO indicador de fallo)
            if detectar_error_reserva(page):
                logger.warning(f"Intento {intento + 1}: Servidor rechazó la reserva - hora ya tomada")
                if intento < max_intentos - 1:
                    espera = (2 ** intento) + random.uniform(0, 0.5)
                    logger.info(f"Esperando {espera:.1f}s antes de reintentar...")
                    time.sleep(espera)
                continue
            
            # VERIFICACIÓN 2: Confirmar que el portal dejó la hora seleccionada
            try:
                boton_seleccionado = page.locator(f"button.timeslot[value='{hora}'][aria-pressed='true']").first
                if boton_seleccionado.is_visible(timeout=300):
                    logger.debug(f"  ✓ Botón {hora} está seleccionado")
                    logger.info(f"✅ Click EXITOSO en hora {hora}")
                    return (True, "Click exitoso")
            except:
                logger.debug(f"  No se pudo verificar el estado seleccionado de {hora}")
            
            logger.warning(f"El portal no confirmó la selección de {hora}")
            if intento < max_intentos - 1:
                espera = (2 ** intento) + random.uniform(0, 0.5)
                time.sleep(espera)
        
        except Exception as e:
            logger.error(f"Intento {intento + 1}: Error en click - {str(e)}")
            if intento < max_intentos - 1:
                espera = (2 ** intento) + random.uniform(0, 0.5)
                logger.info(f"Esperando {espera:.1f}s antes de reintentar...")
                time.sleep(espera)
    
    logger.error(f"❌ Click falló después de {max_intentos} intentos")
    return (False, f"Falló después de {max_intentos} intentos")


def recargar_y_reseleccionar(page):
    """Recarga la página y reselecciona sucursal y servicio con reintentos"""
    max_intentos_recarga = 3
    
    for intento_recarga in range(max_intentos_recarga):
        try:
            logger.info(f"Recargando página (intento {intento_recarga + 1}/{max_intentos_recarga})...")
            page.reload(wait_until="domcontentloaded", timeout=10000)
            
            # Esperar a que carguen los elementos
            page.wait_for_selector(f"text={CONFIG['sucursal']}", timeout=5000, state="visible")
            
            logger.info("Reseleccionando sucursal...")
            try:
                page.locator(f"text={CONFIG['sucursal']}").first.click(force=True, timeout=5000)
            except Exception as e:
                logger.warning(f"Error haciendo click en sucursal: {e}. Reintentando...")
                time.sleep(1)
                page.locator(f"text={CONFIG['sucursal']}").first.click(force=True, timeout=5000)
            
            # Esperar a que aparezca el servicio
            page.wait_for_selector(f"text={CONFIG['servicio']}", timeout=5000, state="visible")
            
            logger.info("Reseleccionando servicio...")
            # Reintentos específicos para el click del servicio
            max_intentos_servicio = 3
            for intento_servicio in range(max_intentos_servicio):
                try:
                    logger.debug(f"  Click en servicio (intento {intento_servicio + 1}/{max_intentos_servicio})...")
                    page.locator(f"text={CONFIG['servicio']}").first.click(force=True, timeout=5000)
                    logger.debug("  ✓ Click en servicio exitoso")
                    break
                except Exception as e:
                    if intento_servicio < max_intentos_servicio - 1:
                        espera = (2 ** intento_servicio) + random.uniform(0, 0.5)
                        logger.warning(f"  Error en click del servicio: {e}")
                        logger.warning(f"  Esperando {espera:.1f}s antes de reintentar...")
                        time.sleep(espera)
                    else:
                        logger.error(f"  Falló click del servicio después de {max_intentos_servicio} intentos")
                        raise
            
            time.sleep(0.5)
            
            logger.info("✓ Página recargada y reseleccionada exitosamente")
            return True
            
        except Exception as e:
            logger.warning(f"Intento de recarga {intento_recarga + 1} falló: {e}")
            
            if intento_recarga < max_intentos_recarga - 1:
                espera = (2 ** intento_recarga) + random.uniform(0, 1)
                logger.warning(f"Esperando {espera:.1f}s antes de reintentar recarga...")
                time.sleep(espera)
            else:
                logger.error(f"Error recargando página después de {max_intentos_recarga} intentos")
                return False
    
    return False


def insistir_en_hora_desaparecida(page, hora, duracion_segundos=None):
    """
    Cuando una hora desaparece justo antes del click, intenta hacer click
    repetidamente durante X segundos esperando que se libere nuevamente
    
    Retorna: (éxito, hora_reservada)
    """
    if duracion_segundos is None:
        duracion_segundos = CONFIG["duracion_insistencia_s"]
    
    intervalo = CONFIG["intervalo_insistencia_ms"] / 1000
    logger.info(f"MODO INSISTENCIA: Haciendo click cada {intervalo}s en hora {hora} durante {duracion_segundos}s...")
    
    tiempo_inicio = time.time()
    intento_insistencia = 0
    
    while (time.time() - tiempo_inicio) < duracion_segundos:
        intento_insistencia += 1
        tiempo_transcurrido = int(time.time() - tiempo_inicio)
        
        try:
            boton = page.locator(f"button.timeslot[value='{hora}'][aria-pressed='false']").first
            if boton.is_visible(timeout=300):
                logger.debug(f"Intento {intento_insistencia}: Hora {hora} disponible ({tiempo_transcurrido}s)")
                
                boton.click(force=True, timeout=2000)
                time.sleep(1)
                
                if not detectar_error_reserva(page):
                    logger.info(f"CLICK EXITOSO EN HORA {hora} DESPUÉS DE {tiempo_transcurrido}s")
                    return (True, hora)
        except Exception as e:
            logger.debug(f"Intento {intento_insistencia}: Error - {str(e)}")
        
        time.sleep(intervalo)
    
    logger.warning(f"No se pudo reservar hora {hora} durante {duracion_segundos}s de insistencia")
    return (False, None)


# ============================================================================
# FUNCIÓN PRINCIPAL
# ============================================================================

def buscar_cita_principal(page):
    """Bucle principal de búsqueda de citas - VERSIÓN MEJORADA"""
    
    URL = CONFIG["url"]
    MAX_INTENTOS = CONFIG["max_intentos_globales"]
    TIMEOUT_HORAS = CONFIG["timeout_horas"]
    
    tiempo_inicio_global = time.time()
    tiempo_limite = tiempo_inicio_global + (TIMEOUT_HORAS * 3600)
    
    logger.info("="*70)
    logger.info("INICIANDO BÚSQUEDA DE CITAS - VERSIÓN MEJORADA")
    logger.info(f"Sucursal: {CONFIG['sucursal']}")
    logger.info(f"Servicio: {CONFIG['servicio']}")
    logger.info(f"Máximo intentos: {MAX_INTENTOS}")
    logger.info(f"Timeout: {TIMEOUT_HORAS} horas")
    logger.info("="*70)
    
    # Inicialización
    page.goto(URL, wait_until="domcontentloaded", timeout=30000)
    
    # Esperar a que cargue la página
    try:
        page.wait_for_selector(f"text={CONFIG['sucursal']}", timeout=10000, state="visible")
    except Exception as e:
        logger.error(f"Página no cargó correctamente: {e}")
        return False
    
    # PASO 1: Seleccionar sucursal
    try:
        page.locator(f"text={CONFIG['sucursal']}").first.click(force=True, timeout=5000)
        page.wait_for_selector(f"text={CONFIG['servicio']}", timeout=5000, state="visible")
    except Exception as e:
        logger.error(f"Error en paso 1: {e}")
        return False
    
    # PASO 2: Seleccionar servicio
    try:
        logger.info("PASO 2: Seleccionando servicio...")
        # Reintentos para el click del servicio
        max_intentos_servicio_inicial = 3
        for intento_srv in range(max_intentos_servicio_inicial):
            try:
                logger.debug(f"  Intento {intento_srv + 1}/{max_intentos_servicio_inicial}...")
                page.locator(f"text={CONFIG['servicio']}").first.click(force=True, timeout=5000)
                logger.info("  ✓ Servicio seleccionado")
                break
            except Exception as e:
                if intento_srv < max_intentos_servicio_inicial - 1:
                    espera = (2 ** intento_srv) + random.uniform(0, 0.5)
                    logger.warning(f"  Error seleccionando servicio: {e}")
                    logger.info(f"  Esperando {espera:.1f}s antes de reintentar...")
                    time.sleep(espera)
                else:
                    raise
        
        time.sleep(1)
    except Exception as e:
        logger.error(f"Error en paso 2 (selección de servicio): {e}")
        return False
    
    # PASO 3: Bucle de detección de horas
    logger.info("PASO 3: Buscando horas disponibles...")
    
    intento = 0
    while intento < MAX_INTENTOS:
        # Verificar timeout global
        if time.time() > tiempo_limite:
            logger.error(f"Timeout global alcanzado ({TIMEOUT_HORAS} horas). Finalizando.")
            return False
        
        intento += 1
        hora_actual = datetime.now().strftime("%H:%M:%S")
        
        # Obtener horas disponibles EXCLUYENDO las bloqueadas
        horas = obtener_horas_disponibles_filtradas(page)
        
        if horas:
            logger.info(f"Intento {intento} ({hora_actual}): {len(horas)} hora(s) - {horas}")
            
            # INTENTAR CADA HORA DISPONIBLE EN ORDEN
            for idx, hora in enumerate(horas, 1):
                logger.info(f"  [{idx}/{len(horas)}] Intentando {hora}...")
                
                # ESTABILIDAD MÁS RÁPIDA
                time.sleep(CONFIG["espera_estabilidad_ms"] / 1000)
                
                # VERIFICAR DISPONIBILIDAD
                if not verificar_disponibilidad_hora(page, hora):
                    logger.debug(f"  Hora {hora} desapareció, siguiente...")
                    bloquear_hora(hora)
                    continue
                
                # HACER CLICK
                exito, mensaje = hacer_click_con_reintentos(page, hora)
                
                if exito:
                    notificar_exito(page, hora, len(horas), intento, horas_intentadas_fallidas)
                    return True
                else:
                    logger.debug(f"  Fallo {hora}, bloqueando...")
                    # Agregar a la lista de horas intentadas fallidas
                    if hora not in horas_intentadas_fallidas:
                        horas_intentadas_fallidas.append(hora)
                    bloquear_hora(hora)
                    continue
            
            # Si llegamos aquí, se intentaron todas las horas sin éxito
            logger.warning(f"Las {len(horas)} horas fallaron. Recargando página...")
            if not recargar_y_reseleccionar(page):
                logger.error("No se pudo recargar la página. Forzando reinicio completo...")
                return False
        else:
            logger.debug(f"Intento {intento}: Sin horas disponibles")
            time.sleep(CONFIG["espera_entre_busquedas_s"])
            
            if not recargar_y_reseleccionar(page):
                logger.error("No se pudo recargar la página tras búsqueda vacía. Forzando reinicio completo...")
                return False
    
    logger.error(f"Se alcanzó el límite de {MAX_INTENTOS} intentos. Finalizando.")
    return False


def notificar_exito(page, hora, num_horas, intentos, horas_fallidas=None):
    """Notifica el éxito de la reserva por todos los canales y registra en estadísticas"""
    global horas_intentadas_fallidas
    
    logger.info("="*70)
    logger.info("CITA RESERVADA EXITOSAMENTE")
    logger.info("="*70)
    
    # Si no se pasan horas fallidas, usar la lista global
    if horas_fallidas is None:
        horas_fallidas = horas_intentadas_fallidas
    
    # REGISTRAR EN ESTADÍSTICAS
    stats = registrar_cita_encontrada(hora, num_horas, intentos, horas_fallidas)
    
    # Alertas y notificaciones
    try:
        winsound.Beep(1500, 300)
        time.sleep(0.2)
        winsound.Beep(1500, 300)
        logger.info("✓ Beeps de alerta ejecutados")
    except Exception as e:
        logger.debug(f"Error en beep: {e}")
    
    logger.info("Iniciando síntesis de voz...")
    hablar(f"Cita encontrada a las {hora}")
    
    mensaje_whatsapp = f"""🎉 CITA ENCONTRADA Y RESERVADA

📋 Servicio: {CONFIG['servicio']}
🏢 Oficina: {CONFIG['sucursal']}
⏰ Hora: {hora}
📊 Total horas disponibles: {num_horas}
🔄 Intentos realizados: {intentos}
✅ Cita Nº {stats['total_citas_encontradas']}

⏳ Completa el formulario en el navegador ahora.
⚠️ Si falla el envío, usa Ctrl+C para guardar los detalles del error."""
    
    logger.info("Enviando mensaje de WhatsApp...")
    logger.info(f"Mensaje: {mensaje_whatsapp}")
    
    exito_whatsapp = enviar_whatsapp(mensaje_whatsapp)
    
    if exito_whatsapp:
        logger.info("✅ WhatsApp ENVIADO EXITOSAMENTE")
    else:
        logger.error("❌ FALLO al enviar WhatsApp - revisar credenciales de Twilio")
    
    # ============================================================================
    # VALIDACIONES DEL FORMULARIO
    # ============================================================================
    
    logger.info("\n" + "="*80)
    logger.info("🔍 VALIDANDO PÁGINA ANTES DE ENVÍO")
    logger.info("="*80)
    
    # 1. DETECTAR reCAPTCHA
    logger.info("\n1️⃣  Detectando reCAPTCHA...")
    tiene_recaptcha = detectar_recaptcha(page)
    
    if tiene_recaptcha:
        logger.warning("⚠️  ¡Página tiene reCAPTCHA!")
        if not esperar_confirmacion_recaptcha(page):
            logger.error("No se pudo resolver reCAPTCHA")
    else:
        logger.info("✅ No hay reCAPTCHA detectado")
    
    # 2. VALIDAR CAMPOS DEL FORMULARIO
    logger.info("\n2️⃣  Validando campos del formulario...")
    campos_vacios, campos_completos = validar_campos_formulario(page)
    
    if campos_vacios:
        logger.warning(f"⚠️  CAMPOS VACÍOS DETECTADOS: {', '.join(campos_vacios)}")
        logger.warning("   Asegúrate de completar estos campos antes de enviar:")
        for campo in campos_vacios:
            logger.warning(f"   - {campo}")
    else:
        logger.info(f"✅ Todos los campos están completos: {', '.join(campos_completos)}")
    
    logger.info("\n" + "="*80)
    logger.info("⏳ ESPERANDO A QUE COMPLETES Y ENVÍES EL FORMULARIO")
    logger.info("="*80)
    logger.info("""
📋 PRÓXIMOS PASOS:
1. Abre el navegador Chrome (ya está abierto)
2. Si falta información, completa los campos marcados arriba
3. Resuelve el reCAPTCHA si aparece
4. Haz click en el botón "Crear cita"
5. Espera la confirmación
6. Presiona Ctrl+C aquí cuando termines
""")
    logger.info("="*80 + "\n")
    
    logger.info("="*70)


def mostrar_resumen_estadisticas():
    """Muestra un resumen de las estadísticas acumuladas"""
    stats = cargar_estadisticas()
    
    logger.info("="*70)
    logger.info("RESUMEN DE ESTADÍSTICAS DE CITAS")
    logger.info("="*70)
    logger.info(f"Total de citas reservadas: {stats['total_citas_encontradas']}")
    
    if stats['historial']:
        logger.info("\nÚltimas 5 citas encontradas:")
        for idx, cita in enumerate(stats['historial'][-5:], 1):
            horas_fallidas = cita.get('horas_intentadas_fallidas', [])
            horas_fallidas_str = ", ".join(horas_fallidas) if horas_fallidas else "ninguna"
            logger.info(f"  {idx}. {cita['timestamp']}")
            logger.info(f"     ✅ Hora reservada: {cita['hora']}")
            logger.info(f"     📊 Horas disponibles: {cita['horas_disponibles']}")
            logger.info(f"     ❌ Horas intentadas (fallidas): {horas_fallidas_str}")
            logger.info(f"     🔄 Total intentos: {cita['total_intentos']}")
    
    logger.info("="*70)


# ============================================================================
# EJECUCIÓN PRINCIPAL
# ============================================================================
if __name__ == "__main__":
    logger.info("Iniciando script de búsqueda de citas...")
    logger.info(f"Configuración: {json.dumps(CONFIG, indent=2)}")
    
    # Mostrar estadísticas previas
    stats_previas = cargar_estadisticas()
    logger.info(f"Citas encontradas anteriormente: {stats_previas['total_citas_encontradas']}")
    
    with sync_playwright() as p:
        # Seleccionar navegador
        navegador_nombre = CONFIG["navegador"].lower()
        if navegador_nombre == "firefox":
            logger.info("Iniciando navegador: Firefox")
            browser = p.firefox.launch(headless=False)
            context = browser.new_context(locale="es-ES", timezone_id="Europe/Madrid")
        elif navegador_nombre == "webkit":
            logger.info("Iniciando navegador: WebKit (Safari)")
            browser = p.webkit.launch(headless=False)
            context = browser.new_context(locale="es-ES", timezone_id="Europe/Madrid")
        else:
            logger.info("Iniciando navegador: Chromium - Idioma: Español (Evasión Anti-bot)")
            browser = p.chromium.launch(
                headless=False,
                args=[
                    "--lang=es-ES,es",
                    "--disable-blink-features=AutomationControlled",
                ]
            )
            context = browser.new_context(
                locale="es-ES",
                timezone_id="Europe/Madrid",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
        
        page = context.new_page()
        
        # Ocultar señales de automatización (Esencial para reCAPTCHA v3 invisible)
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['es-ES', 'es'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """)
        
        try:
            reinicio_intento = 0
            while True:
                reinicio_intento += 1
                if reinicio_intento > 1:
                    logger.info(f"🔄 Iniciando intento de recuperación/reinicio #{reinicio_intento}...")
                    # Limpieza rápida: ir a about:blank para liberar memoria y recursos antes de volver a empezar
                    try:
                        page.goto("about:blank", timeout=5000)
                    except:
                        pass
                    time.sleep(3)
                
                try:
                    resultado = buscar_cita_principal(page)
                    if resultado:
                        logger.info("Script finalizado con éxito (Cita gestionada)")
                        break
                    else:
                        logger.warning("La búsqueda de citas falló o se detectó un bloqueo. Reiniciando flujo completo en 5 segundos...")
                        time.sleep(5)
                except Exception as e:
                    logger.error(f"Error durante la búsqueda (posible bloqueo): {e}. Reiniciando en 5 segundos...")
                    time.sleep(5)
                    
        except KeyboardInterrupt:
            logger.info("Búsqueda cancelada por usuario")
        except Exception as e:
            logger.error(f"Error fatal: {e}", exc_info=True)
        finally:
            # Mostrar resumen de estadísticas
            mostrar_resumen_estadisticas()
            
            logger.info("Navegador abierto. Presiona Ctrl+C para cerrar.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                logger.info("Cerrando navegador...")
                try:
                    browser.close()
                except KeyboardInterrupt:
                    logger.info("Forzando cierre...")
                    try:
                        browser.close()
                    except:
                        pass
                except Exception as e:
                    logger.warning(f"Error cerrando navegador: {e}")
            finally:
                flush_logs()
                logger.info("Logs guardados en 'cita_buscador.log'")