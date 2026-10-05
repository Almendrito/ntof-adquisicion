"""
captura_tds684a.py

Qué hace:
    Se conecta por GPIB al osciloscopio Tektronix TDS 684A y lee el registro
    completo de cada canal que está en pantalla, es decir, todos los puntos
    que adquirió el osciloscopio (hasta 15 000 por canal). También lee las
    escalas de la retícula (V/div de cada canal y s/div de la base de tiempo),
    grafica las trazas con la misma retícula de 10 × 8 divisiones que muestra
    el osciloscopio y guarda todo en un archivo .csv.

Cómo se ejecuta:
    python captura_tds684a.py

Qué necesita:
    - Python 3.9 o más nuevo con tkinter (viene incluido en Python para
      Windows y macOS).
    - Librerías:  pip install pyvisa numpy matplotlib
    - El driver del adaptador GPIB a USB:
        * National Instruments (GPIB-USB-HS): NI-488.2 y NI-VISA.
        * Keysight o Agilent (82357B): Keysight IO Libraries Suite.
    - El puerto GPIB del osciloscopio en modo Talk/Listen (menú UTILITY,
      sección I/O). La dirección de fábrica es 1.

Qué produce:
    - Un archivo .csv con las columnas indice, tiempo_s y una columna de
      voltaje por canal (CH1_V, CH2_V, ...). Antes de los datos hay líneas que
      empiezan con # con los datos del disparo, las escalas, el preámbulo de
      cada canal y las verificaciones.
    - Una imagen .png con el mismo nombre del .csv, con la vista de pantalla
      y los datos del disparo como título.

Cómo leer el .csv después:
    Con pandas:  datos = pandas.read_csv("archivo.csv", comment="#")
    Con numpy:   datos = numpy.genfromtxt("archivo.csv", delimiter=",", names=True, comments="#")
    En Excel en español conviene abrirlo con Datos > Desde texto/CSV,
    porque el archivo usa punto decimal y coma como separador.

Pasos en la ventana:
    1. «Buscar» para encontrar el osciloscopio (o escribe GPIB0::1::INSTR).
    2. Detén el osciloscopio con RUN/STOP, para que todos los canales
       correspondan al mismo disparo.
    3. «Leer osciloscopio». Puede tardar un poco por canal porque los datos
       viajan como texto, que es lo más robusto.
    4. Revisa las verificaciones del panel y la vista. «Guardar CSV» pregunta
       el número de disparo, la diferencia de potencial (kV), la distancia del
       detector (cm) y el gas, y los escribe en el encabezado del .csv.
       Al guardar el siguiente disparo los campos parten con esos mismos
       valores y el número de disparo avanzado en uno.

Cómo se convierten los datos (fórmulas del preámbulo de Tektronix):
    tiempo_s  = XZERO + XINCR × (indice − PT_OFF)
    voltaje_V = YZERO + YMULT × (nivel − YOFF)
    divisiones en pantalla = nivel / niveles_por_division
    donde "nivel" es el número entero que entrega el digitalizador.

Herramientas avanzadas usadas (ver los comentarios NOTA en el código):
    - Hilos (threading) para hablar con el osciloscopio sin congelar la ventana.
    - La clase App hereda de ttk.Frame, igual que en la plantilla de
      interfaces. Heredar significa que App es un contenedor de tkinter con
      todo lo que este ya sabe hacer, más los métodos que le agregamos.
"""

# Para armar el nombre del .png a partir del nombre del .csv
import os
# Para saber si el programa corre en Windows (nitidez de la ventana)
import sys
# Para la fecha y hora del nombre del archivo y del encabezado del .csv
import time
# Cola segura para pasar resultados desde los hilos a la ventana
import queue
# Para hablar con el osciloscopio en un hilo aparte y no congelar la ventana
import threading
# Ventanas, botones y campos de texto
import tkinter as tk
from tkinter import ttk, font, filedialog, messagebox

# Cálculos sobre los registros completos (tiempos y voltajes de todos los puntos)
import numpy as np
# Gráfico dentro de la ventana
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
# Para mostrar números con prefijo (ns, mV, ...)
from matplotlib.ticker import EngFormatter

# PyVISA habla con el adaptador GPIB. Si no está instalado, la ventana igual
# abre y avisa qué instalar al intentar conectarse.
try:
    import pyvisa
except ImportError:
    pyvisa = None


# ─── Tokens de diseño (de la plantilla de interfaces, no modificar) ────────
COLORS = {
    "bg":        "#F5F7FA",  # fondo de la ventana
    "surface":   "#FFFFFF",  # tarjetas y paneles
    "border":    "#E2E6EB",  # líneas finas
    "text":      "#1C2733",  # texto principal
    "muted":     "#6B7683",  # etiquetas y texto secundario
    "accent":    "#2B5FB0",  # color de acción (un solo acento)
    "accent_fg": "#FFFFFF",
    "ok":        "#2E9E6B",  # conectado o correcto
    "warn":      "#C8860B",  # advertencia
    "error":     "#D64545",  # falla
}

# Escala de espaciado en píxeles (múltiplos de 4). Usar siempre estas constantes.
PAD_S, PAD_M, PAD_L, PAD_XL = 4, 8, 16, 24


# ─── Constantes del osciloscopio ───────────────────────────────────────────

# Divisiones de la retícula del TDS 684A: 10 horizontales y 8 verticales.
# Fuente: manual de servicio TDS 684A/744A/784A (retícula de 501 × 401 píxeles).
DIVISIONES_HORIZONTALES = 10
DIVISIONES_VERTICALES = 8

# Mitad de la altura de la pantalla, en divisiones (la pantalla va de −4 a +4)
MITAD_DIVISIONES_VERTICALES = DIVISIONES_VERTICALES // 2

# Niveles del digitalizador por división vertical cuando cada punto ocupa
# 1 byte. Los Tektronix de la serie TDS usan 25 niveles por división: los
# 256 niveles del conversor de 8 bits cubren 10,24 divisiones. Con 2 bytes
# por punto cada nivel se subdivide en 256, así que hay 25 × 256 por división.
# El programa verifica este valor comparando con la perilla V/div.
NIVELES_POR_DIVISION_8_BITS = 25

# Bytes por punto que se piden al osciloscopio. Con 2 bytes no se pierde
# resolución cuando el osciloscopio promedia (modo Average da más de 8 bits).
BYTES_POR_PUNTO = 2

# Longitud de registro máxima del TDS 684A, en puntos por canal.
# Fuente: manual de servicio (registros de 500, 1000, 2500, 5000 y 15000).
LONGITUD_MAXIMA_REGISTRO = 15000

# Altura, en divisiones desde el centro, desde la cual se considera que la
# señal saturó el digitalizador. El conversor llega a ±5,12 divisiones.
DIVISIONES_DE_SATURACION = 5.0

# Canales que se pueden leer
CANALES = ["CH1", "CH2", "CH3", "CH4"]

# Gases que aparecen en la lista desplegable de "Datos del disparo". Se
# puede escribir cualquier otro; la lista solo ahorra tipeo.
GASES_FRECUENTES = ["D2", "H2", "He", "Ar", "Ne", "N2", "Aire"]

# Color de cada canal en el gráfico (paleta de la plantilla de interfaces)
COLORES_DE_CANALES = {
    "CH1": "#2B5FB0",
    "CH2": "#2E9E6B",
    "CH3": "#C8860B",
    "CH4": "#8452C4",
}


# ─── Constantes de la comunicación ─────────────────────────────────────────

# Dirección VISA por defecto: primer adaptador GPIB (GPIB0) y dirección 1,
# que es la dirección de fábrica del osciloscopio.
RECURSO_POR_DEFECTO = "GPIB0::1::INSTR"

# Tiempo máximo de espera para una respuesta corta, en milisegundos.
TIEMPO_ESPERA_CONSULTA_MS = 5000

# Tiempo máximo de espera para recibir la curva completa, en milisegundos.
# Un registro de 15 000 puntos como texto pesa cerca de 100 kB; 3 minutos
# deja mucho margen aunque el adaptador sea lento.
TIEMPO_ESPERA_CURVA_MS = 180000

# Claves del preámbulo que el programa necesita para convertir los datos.
CLAVES_NECESARIAS_DEL_PREAMBULO = ["XINCR", "PT_OFF", "YMULT", "YOFF", "YZERO"]

# Diferencia relativa máxima para dar por buena una verificación (1 %).
TOLERANCIA_DE_VERIFICACION = 0.01

# Fracción de división bajo la cual una marca del eje de tiempo se considera
# exactamente cero (solo para que la etiqueta no muestre restos de redondeo).
FRACCION_DE_DIVISION_DESPRECIABLE = 1e-6

# Prefijos para las unidades de los ejes, de menor a mayor: (factor, prefijo).
# La unidad va una sola vez en el título del eje, por ejemplo "tiempo (ns)",
# y las marcas muestran solo números.
PREFIJOS_DE_UNIDAD = [
    (1e-12, "p"),
    (1e-9, "n"),
    (1e-6, "µ"),
    (1e-3, "m"),
    (1.0, ""),
    (1e3, "k"),
]

# Cada cuánto la ventana revisa si un hilo terminó, en milisegundos.
INTERVALO_REVISION_MS = 100


# ─── Clases ────────────────────────────────────────────────────────────────

class ConexionOsciloscopio:
    """
    Conversación con el osciloscopio por GPIB usando PyVISA.

    Se abre al crearla y se cierra con cerrar(). La crean las funciones de
    lectura, que siempre la cierran al terminar aunque haya errores, para no
    dejar el adaptador tomado.

    Atributos
    ---------
    instrumento : pyvisa.resources.Resource
        El osciloscopio abierto por VISA.
    """

    def __init__(self, nombre_recurso):
        """
        Abre el osciloscopio y lo deja respondiendo sin encabezados.

        Parámetros
        ----------
        nombre_recurso : str
            Dirección VISA del osciloscopio, por ejemplo "GPIB0::1::INSTR".

        Errores
        -------
        RuntimeError si no se puede abrir la dirección.
        """
        # Paso 1: abrir el instrumento
        administrador = crear_administrador_visa()
        try:
            self.instrumento = administrador.open_resource(nombre_recurso)
        except pyvisa.errors.VisaIOError as error:
            raise RuntimeError(f"No se pudo abrir {nombre_recurso}. Revisa el cable, que el "
                               f"osciloscopio esté encendido y su dirección GPIB.\n\n{error}")

        # Paso 2: configurar tiempos de espera y fin de línea
        self.instrumento.timeout = TIEMPO_ESPERA_CONSULTA_MS
        self.instrumento.read_termination = "\n"
        self.instrumento.write_termination = "\n"

        # Paso 3: borrar errores viejos y pedir respuestas sin encabezado.
        # Con HEADER OFF el osciloscopio responde "2.0E-1" en vez de
        # ":CH1:SCALE 2.0E-1", y así la respuesta se convierte directo en número.
        self.escribir("*CLS")
        self.escribir("HEADER OFF")

    def escribir(self, comando):
        """
        Envía un comando que no tiene respuesta.

        Parámetros
        ----------
        comando : str
            Comando en el lenguaje del osciloscopio, por ejemplo "DATA:SOURCE CH1".
        """
        self.instrumento.write(comando)

    def consultar_texto(self, comando):
        """
        Envía una consulta y retorna la respuesta como texto.

        Parámetros
        ----------
        comando : str
            Consulta terminada en "?", por ejemplo "*IDN?".

        Retorna
        -------
        str
            Respuesta sin espacios ni saltos de línea al inicio o al final.

        Errores
        -------
        pyvisa.errors.VisaIOError si el osciloscopio no responde a tiempo.
        """
        respuesta = self.instrumento.query(comando)
        return respuesta.strip()

    def consultar_numero(self, comando):
        """
        Envía una consulta cuya respuesta es un número.

        Parámetros
        ----------
        comando : str
            Consulta, por ejemplo "CH1:SCALE?".

        Retorna
        -------
        float
            El número recibido, en las unidades del osciloscopio (V, s, div o %).

        Errores
        -------
        RuntimeError si la respuesta no es un número.
        pyvisa.errors.VisaIOError si el osciloscopio no responde a tiempo.
        """
        respuesta = self.consultar_texto(comando)
        return extraer_numero_de_respuesta(respuesta, comando)

    def consultar_opcional(self, comando):
        """
        Igual que consultar_texto, pero retorna None si el osciloscopio no responde.

        Sirve para datos que solo van al encabezado del .csv (por ejemplo el
        acoplamiento): si este modelo no conoce la consulta, se sigue igual.

        Parámetros
        ----------
        comando : str
            Consulta.

        Retorna
        -------
        str o None
        """
        try:
            return self.consultar_texto(comando)
        except pyvisa.errors.VisaIOError:
            # La consulta falló; borramos el error para que no afecte a las siguientes
            self.escribir("*CLS")
            return None

    def consultar_numero_opcional(self, comando):
        """
        Igual que consultar_numero, pero retorna None si no hay respuesta válida.

        Parámetros
        ----------
        comando : str
            Consulta.

        Retorna
        -------
        float o None
        """
        respuesta = self.consultar_opcional(comando)
        if respuesta is None:
            return None
        try:
            return extraer_numero_de_respuesta(respuesta, comando)
        except RuntimeError:
            return None

    def leer_preambulo(self, canal):
        """
        Lee el preámbulo del canal: los números que convierten los datos en s y V.

        Se pide con encabezados y nombres completos (HEADER ON y VERBOSE ON)
        para leer cada valor por su nombre y no por su posición en la lista,
        que puede cambiar entre modelos.

        Parámetros
        ----------
        canal : str
            "CH1", "CH2", "CH3" o "CH4". Debe coincidir con DATA:SOURCE.

        Retorna
        -------
        dict de str a str
            Por ejemplo {"XINCR": "2.0E-10", "YMULT": "3.125E-5", ...}.

        Errores
        -------
        RuntimeError si falta algún valor indispensable.
        """
        self.escribir("HEADER ON")
        self.escribir("VERBOSE ON")
        try:
            # Paso 1: preámbulo general (incluye el del canal elegido en DATA:SOURCE)
            preambulo = interpretar_preambulo(self.consultar_texto("WFMPRE?"))

            # Paso 2: si faltan valores, pedir explícitamente el preámbulo del canal
            falta_alguna_clave = False
            for clave in CLAVES_NECESARIAS_DEL_PREAMBULO:
                if clave not in preambulo:
                    falta_alguna_clave = True
            if falta_alguna_clave:
                preambulo_del_canal = interpretar_preambulo(self.consultar_texto(f"WFMPRE:{canal}?"))
                for clave in preambulo_del_canal:
                    preambulo[clave] = preambulo_del_canal[clave]
        finally:
            # Paso 3: volver a respuestas sin encabezado pase lo que pase
            self.escribir("HEADER OFF")

        # Paso 4: confirmar que esté todo
        for clave in CLAVES_NECESARIAS_DEL_PREAMBULO:
            if clave not in preambulo:
                raise RuntimeError(f"El preámbulo de {canal} no trae {clave}. "
                                   "Sin ese valor no se pueden convertir los datos.")
        return preambulo

    def leer_curva(self):
        """
        Pide la curva (los niveles de todos los puntos) en el formato ya configurado.

        Retorna
        -------
        str
            Los niveles separados por comas, por ejemplo "-12800,-12544,...".

        Errores
        -------
        pyvisa.errors.VisaIOError si no llega completa antes de TIEMPO_ESPERA_CURVA_MS.
        """
        # Esta respuesta es larga: alargamos el tiempo de espera solo para ella
        self.instrumento.timeout = TIEMPO_ESPERA_CURVA_MS
        try:
            texto = self.consultar_texto("CURVE?")
        finally:
            self.instrumento.timeout = TIEMPO_ESPERA_CONSULTA_MS
        return texto

    def cerrar(self):
        """Cierra la conexión con el osciloscopio."""
        self.instrumento.close()


class AjustesHorizontales:
    """
    Base de tiempo y adquisición del osciloscopio al momento de la lectura.

    La crea leer_ajustes_horizontales y se guarda dentro de LecturaDelOsciloscopio.

    Atributos
    ---------
    segundos_por_division_s : float
        Base de tiempo de la pantalla (la perilla s/div), en s por división.
    longitud_registro : int
        Puntos que guarda el osciloscopio por canal.
    posicion_trigger_porcentaje : float o None
        Dónde está el trigger dentro del registro, en % del registro.
    posicion_horizontal_porcentaje : float o None
        Posición horizontal: qué parte del registro se ve, en % del registro.
    modo_adquisicion : str o None
        Por ejemplo "SAMPLE", "AVERAGE" o "ENVELOPE".
    numero_de_promedios : str o None
        Cuántas adquisiciones promedia el modo Average.
    esta_adquiriendo : bool
        True si el osciloscopio estaba en RUN (no detenido) al leer.
    """

    def __init__(self):
        """Crea los ajustes vacíos; los llena leer_ajustes_horizontales."""
        self.segundos_por_division_s = 0.0
        self.longitud_registro = 0
        self.posicion_trigger_porcentaje = None
        self.posicion_horizontal_porcentaje = None
        self.modo_adquisicion = None
        self.numero_de_promedios = None
        self.esta_adquiriendo = False


class TrazaDeCanal:
    """
    Todo lo leído de un canal: sus escalas, su preámbulo y sus puntos.

    La crea leer_canal. La ventana la usa para graficar y para el .csv.

    Atributos
    ---------
    nombre : str
        "CH1", "CH2", "CH3" o "CH4".
    volts_por_division_V : float
        Escala vertical (perilla V/div), en V por división.
    posicion_div : float
        Posición vertical, en divisiones.
    offset_V : float
        Offset vertical, en V.
    acoplamiento, impedancia : str o None
        Por ejemplo "DC" y "FIFTY". Solo informativos.
    atenuacion_sonda : float o None
        Atenuación de la sonda que el osciloscopio tiene configurada (1 para
        1X, 10 para 10X). None si el osciloscopio no respondió.
    preambulo : dict de str a str
        Valores del preámbulo leídos por nombre (XINCR, YMULT, ...).
    niveles : numpy.ndarray de float
        Número entero que entregó el digitalizador para cada punto.
    tiempos_s : numpy.ndarray de float
        Tiempo de cada punto medido desde el trigger, en s.
    voltajes_V : numpy.ndarray de float
        Voltaje de cada punto, en V.
    niveles_por_division : float
        Niveles del digitalizador que caben en una división vertical.
    divisiones_en_pantalla : numpy.ndarray de float
        Altura de cada punto en la pantalla, en divisiones desde el centro.
    """

    def __init__(self, nombre):
        """
        Crea la traza vacía de un canal.

        Parámetros
        ----------
        nombre : str
            "CH1", "CH2", "CH3" o "CH4".
        """
        self.nombre = nombre
        self.volts_por_division_V = 0.0
        self.posicion_div = 0.0
        self.offset_V = 0.0
        self.acoplamiento = None
        self.impedancia = None
        self.atenuacion_sonda = None
        self.preambulo = {}
        self.niveles = None
        self.tiempos_s = None
        self.voltajes_V = None
        self.niveles_por_division = 0.0
        self.divisiones_en_pantalla = None

    def valor_del_preambulo(self, clave, valor_si_falta):
        """
        Retorna un valor numérico del preámbulo.

        Parámetros
        ----------
        clave : str
            Nombre del valor, por ejemplo "XINCR".
        valor_si_falta : float
            Valor a usar si el preámbulo no trae esa clave (por ejemplo,
            XZERO no existe en todos los modelos y vale 0).

        Retorna
        -------
        float
        """
        if clave not in self.preambulo:
            return valor_si_falta
        return extraer_numero_de_respuesta(self.preambulo[clave], "preámbulo " + clave)

    def nivel_de_cero_en_divisiones(self):
        """
        Retorna
        -------
        float
            Altura de 0 V en la pantalla, en divisiones desde el centro. Es
            donde el osciloscopio dibuja la flecha de tierra del canal.
        """
        multiplicador_V = self.valor_del_preambulo("YMULT", 1.0)
        offset_niveles = self.valor_del_preambulo("YOFF", 0.0)
        cero_V = self.valor_del_preambulo("YZERO", 0.0)
        # Despejando la fórmula de voltaje con voltaje = 0
        nivel_de_cero = offset_niveles + (0.0 - cero_V) / multiplicador_V
        return nivel_de_cero / self.niveles_por_division

    def numero_de_puntos(self):
        """
        Retorna
        -------
        int
            Cuántos puntos tiene la traza.
        """
        return len(self.niveles)


class LecturaDelOsciloscopio:
    """
    Resultado completo de una lectura: identificación, base de tiempo y canales.

    La crea leer_osciloscopio y la ventana la guarda para graficar y exportar.

    Atributos
    ---------
    recurso : str
        Dirección VISA usada.
    identificacion : str
        Respuesta a *IDN? (marca, modelo, número de serie y firmware).
    horizontal : AjustesHorizontales
        Base de tiempo y adquisición.
    canales : lista de TrazaDeCanal
        Canales leídos, en orden.
    avisos : lista de str
        Problemas encontrados durante la lectura (canales apagados, etc.).
    fecha : str
        Fecha y hora de la lectura.
    """

    def __init__(self, recurso):
        """
        Crea una lectura vacía.

        Parámetros
        ----------
        recurso : str
            Dirección VISA del osciloscopio.
        """
        self.recurso = recurso
        self.identificacion = ""
        self.horizontal = AjustesHorizontales()
        self.canales = []
        self.avisos = []
        self.fecha = time.strftime("%Y-%m-%d %H:%M:%S")


class DatosDelDisparo:
    """
    Datos del experimento que el usuario escribe al guardar cada disparo.

    La crea la ventana DialogoDatosDelDisparo y se escribe en el encabezado
    del .csv y en el título de la imagen .png.

    Atributos
    ---------
    numero_de_disparo : str
        Número o nombre del disparo, tal como lo escribió el usuario ("" si no lo escribió).
    diferencia_de_potencial_kV : float o None
        Diferencia de potencial aplicada, en kV. None si no se escribió.
    distancia_detector_cm : float o None
        Distancia del detector, en cm. None si no se escribió.
    gas : str
        Gas usado, por ejemplo "D2" ("" si no se escribió).
    """

    def __init__(self, numero_de_disparo, diferencia_de_potencial_kV, distancia_detector_cm, gas):
        """
        Guarda los datos del disparo.

        Parámetros
        ----------
        numero_de_disparo : str
        diferencia_de_potencial_kV : float o None
            En kV.
        distancia_detector_cm : float o None
            En cm.
        gas : str
        """
        self.numero_de_disparo = numero_de_disparo
        self.diferencia_de_potencial_kV = diferencia_de_potencial_kV
        self.distancia_detector_cm = distancia_detector_cm
        self.gas = gas

    def lineas_de_encabezado(self):
        """
        Retorna
        -------
        lista de str
            Una línea por dato, para el encabezado del .csv. Lo que no se
            escribió aparece como "sin dato".
        """
        lineas = []
        lineas.append("disparo: " + texto_o_sin_dato(self.numero_de_disparo))
        lineas.append("diferencia_de_potencial_kV: " + numero_o_sin_dato(self.diferencia_de_potencial_kV))
        lineas.append("distancia_detector_cm: " + numero_o_sin_dato(self.distancia_detector_cm))
        lineas.append("gas: " + texto_o_sin_dato(self.gas))
        return lineas

    def resumen_corto(self):
        """
        Retorna
        -------
        str
            Por ejemplo "Disparo 123 · 25 kV · 150 cm · D2", con solo los datos escritos.
        """
        partes = []
        if self.numero_de_disparo != "":
            partes.append("Disparo " + self.numero_de_disparo)
        if self.diferencia_de_potencial_kV is not None:
            partes.append(f"{self.diferencia_de_potencial_kV:g} kV")
        if self.distancia_detector_cm is not None:
            partes.append(f"{self.distancia_detector_cm:g} cm")
        if self.gas != "":
            partes.append(self.gas)
        return "  ·  ".join(partes)


# ─── Funciones: comunicación ───────────────────────────────────────────────

def crear_administrador_visa():
    """
    Crea el administrador de PyVISA, que es el que encuentra y abre instrumentos.

    Primero intenta con la biblioteca VISA instalada (NI-VISA o Keysight).
    Si no hay ninguna, intenta con pyvisa-py, la versión escrita en Python.

    Retorna
    -------
    pyvisa.ResourceManager

    Errores
    -------
    RuntimeError si PyVISA no está instalado o no hay ninguna biblioteca VISA.
    """
    if pyvisa is None:
        raise RuntimeError("Falta PyVISA. Instálalo con:\n\npip install pyvisa")
    try:
        return pyvisa.ResourceManager()
    except (ValueError, OSError):
        pass
    try:
        return pyvisa.ResourceManager("@py")
    except (ValueError, OSError):
        raise RuntimeError("No se encontró ninguna biblioteca VISA. Instala el driver de tu "
                           "adaptador GPIB: NI-488.2 y NI-VISA (National Instruments) o "
                           "Keysight IO Libraries Suite (Keysight o Agilent).")


def buscar_instrumentos():
    """
    Busca los instrumentos conectados.

    Retorna
    -------
    lista de str
        Direcciones VISA encontradas, primero las GPIB.

    Errores
    -------
    RuntimeError si no hay biblioteca VISA.
    """
    administrador = crear_administrador_visa()
    encontrados = administrador.list_resources()

    # Ponemos primero las direcciones GPIB, que son las que interesan aquí
    direcciones_gpib = []
    otras_direcciones = []
    for direccion in encontrados:
        if direccion.upper().startswith("GPIB"):
            direcciones_gpib.append(direccion)
        else:
            otras_direcciones.append(direccion)
    return direcciones_gpib + otras_direcciones


def extraer_numero_de_respuesta(respuesta, comando):
    """
    Convierte la respuesta del osciloscopio en un número.

    Acepta respuestas con o sin encabezado: "2.0E-1" o ":CH1:SCALE 2.0E-1".
    Si viene el encabezado, se usa lo que está después del último espacio.

    Parámetros
    ----------
    respuesta : str
        Texto recibido.
    comando : str
        Consulta que se hizo, solo para el mensaje de error.

    Retorna
    -------
    float

    Errores
    -------
    RuntimeError si no es un número.
    """
    partes = respuesta.strip().split(" ")
    ultima_parte = partes[-1]
    # Algunos valores llegan entre comillas
    ultima_parte = ultima_parte.strip('"')
    try:
        return float(ultima_parte)
    except ValueError:
        raise RuntimeError(f"Respuesta inesperada a {comando}: «{respuesta}»")


def interpretar_preambulo(texto):
    """
    Separa la respuesta de WFMPRE? (con HEADER ON y VERBOSE ON) en un diccionario.

    Ejemplo de respuesta:
        :WFMPRE:BYT_NR 2;BIT_NR 16;ENCDG ASCII;:WFMPRE:CH1:WFID "Ch1, DC...";NR_PT 500;XINCR 2.0E-10
    Resultado:
        {"BYT_NR": "2", "BIT_NR": "16", "ENCDG": "ASCII", "WFID": "Ch1, DC...", ...}

    Parámetros
    ----------
    texto : str
        Respuesta completa del osciloscopio.

    Retorna
    -------
    dict de str a str
        Cada nombre (la última parte del encabezado, en mayúsculas) con su valor.
    """
    preambulo = {}
    segmentos = texto.split(";")
    for segmento in segmentos:
        segmento = segmento.strip()
        # Cada segmento es "NOMBRE valor"; los que no tienen espacio no traen valor
        posicion_del_espacio = segmento.find(" ")
        if posicion_del_espacio < 0:
            continue
        encabezado = segmento[:posicion_del_espacio]
        valor = segmento[posicion_del_espacio + 1:].strip()
        # Del encabezado ":WFMPRE:CH1:XINCR" nos quedamos con "XINCR"
        partes_del_encabezado = encabezado.split(":")
        clave = partes_del_encabezado[-1].upper()
        preambulo[clave] = valor.strip('"')
    return preambulo


def convertir_curva_a_niveles(texto):
    """
    Convierte la respuesta de CURVE? en un arreglo de niveles.

    Parámetros
    ----------
    texto : str
        Niveles separados por comas, por ejemplo "-12800,-12544,0,256".
        Si viene con encabezado (":CURVE -12800,..."), se descarta.

    Retorna
    -------
    numpy.ndarray de float
        Un nivel por punto, en el orden del registro.

    Errores
    -------
    RuntimeError si algún valor no es un número entero.
    """
    # Paso 1: quitar el encabezado si viene (todo lo anterior al primer espacio)
    limpio = texto.strip()
    if " " in limpio:
        posicion_del_espacio = limpio.find(" ")
        limpio = limpio[posicion_del_espacio + 1:]

    # Paso 2: convertir cada valor
    niveles = []
    for valor_texto in limpio.split(","):
        try:
            niveles.append(int(valor_texto))
        except ValueError:
            raise RuntimeError(f"La curva trae un valor que no es entero: «{valor_texto}»")
    return np.array(niveles, dtype=np.float64)


def leer_ajustes_horizontales(conexion):
    """
    Lee la base de tiempo, la longitud de registro y el modo de adquisición.

    Parámetros
    ----------
    conexion : ConexionOsciloscopio

    Retorna
    -------
    AjustesHorizontales
    """
    ajustes = AjustesHorizontales()

    # Paso 1: valores indispensables (si fallan, la lectura no sirve)
    ajustes.segundos_por_division_s = conexion.consultar_numero("HORIZONTAL:MAIN:SCALE?")
    longitud = conexion.consultar_numero("HORIZONTAL:RECORDLENGTH?")
    ajustes.longitud_registro = int(round(longitud))

    # Paso 2: valores informativos (si este modelo no los conoce, quedan en None)
    ajustes.posicion_trigger_porcentaje = conexion.consultar_numero_opcional("HORIZONTAL:TRIGGER:POSITION?")
    ajustes.posicion_horizontal_porcentaje = conexion.consultar_numero_opcional("HORIZONTAL:POSITION?")
    ajustes.modo_adquisicion = conexion.consultar_opcional("ACQUIRE:MODE?")
    ajustes.numero_de_promedios = conexion.consultar_opcional("ACQUIRE:NUMAVG?")
    estado = conexion.consultar_numero_opcional("ACQUIRE:STATE?")
    if estado is not None and estado > 0:
        ajustes.esta_adquiriendo = True
    return ajustes


def canal_esta_en_pantalla(conexion, canal):
    """
    Pregunta si el canal está encendido en la pantalla del osciloscopio.

    Parámetros
    ----------
    conexion : ConexionOsciloscopio
    canal : str
        "CH1", "CH2", "CH3" o "CH4".

    Retorna
    -------
    bool
        True si está encendido. Si el osciloscopio no responde, se asume que sí.
    """
    estado = conexion.consultar_numero_opcional(f"SELECT:{canal}?")
    if estado is None:
        return True
    return estado > 0


def calcular_niveles_por_division(bytes_por_punto):
    """
    Calcula cuántos niveles del digitalizador caben en una división vertical.

    Parámetros
    ----------
    bytes_por_punto : int
        1 o 2, según el preámbulo (BYT_NR).

    Retorna
    -------
    float
        25 con 1 byte y 25 × 256 = 6400 con 2 bytes.
    """
    # Cada byte extra subdivide cada nivel en 256
    factor = 256 ** (bytes_por_punto - 1)
    return float(NIVELES_POR_DIVISION_8_BITS * factor)


def calcular_unidades_de_la_traza(traza):
    """
    Llena tiempos_s, voltajes_V y divisiones_en_pantalla a partir de los niveles.

    Fórmulas del preámbulo (operan sobre todos los puntos a la vez):
        tiempo  = XZERO + XINCR × (indice − PT_OFF)
        voltaje = YZERO + YMULT × (nivel − YOFF)
        altura en pantalla = nivel / niveles_por_division

    Parámetros
    ----------
    traza : TrazaDeCanal
        Debe traer niveles y preambulo. Se modifica en el lugar.
    """
    # Paso 1: valores del preámbulo
    intervalo_de_muestreo_s = traza.valor_del_preambulo("XINCR", 0.0)
    indice_del_trigger = traza.valor_del_preambulo("PT_OFF", 0.0)
    tiempo_inicial_s = traza.valor_del_preambulo("XZERO", 0.0)
    multiplicador_V = traza.valor_del_preambulo("YMULT", 0.0)
    offset_niveles = traza.valor_del_preambulo("YOFF", 0.0)
    cero_V = traza.valor_del_preambulo("YZERO", 0.0)
    bytes_por_punto = int(traza.valor_del_preambulo("BYT_NR", BYTES_POR_PUNTO))

    # Paso 2: tiempo de cada punto (indice parte en 0)
    indices = np.arange(traza.numero_de_puntos())
    traza.tiempos_s = tiempo_inicial_s + intervalo_de_muestreo_s * (indices - indice_del_trigger)

    # Paso 3: voltaje de cada punto
    traza.voltajes_V = cero_V + multiplicador_V * (traza.niveles - offset_niveles)

    # Paso 4: altura en la pantalla. El nivel 0 es la línea central de la pantalla.
    traza.niveles_por_division = calcular_niveles_por_division(bytes_por_punto)
    traza.divisiones_en_pantalla = traza.niveles / traza.niveles_por_division


def leer_canal(conexion, canal, longitud_registro):
    """
    Lee un canal completo: escalas, preámbulo y todos los puntos del registro.

    Parámetros
    ----------
    conexion : ConexionOsciloscopio
    canal : str
        "CH1", "CH2", "CH3" o "CH4".
    longitud_registro : int
        Puntos del registro; se piden todos, del 1 al último.

    Retorna
    -------
    TrazaDeCanal
        Con todo calculado. Si el canal está en modo Envelope, retorna la
        traza sin niveles (niveles = None) para que quien llama avise.
    """
    traza = TrazaDeCanal(canal)

    # Paso 1: escalas de la retícula para este canal (los "intervalos de los cuadros")
    traza.volts_por_division_V = conexion.consultar_numero(f"{canal}:SCALE?")
    traza.posicion_div = conexion.consultar_numero(f"{canal}:POSITION?")
    traza.offset_V = conexion.consultar_numero(f"{canal}:OFFSET?")
    traza.acoplamiento = conexion.consultar_opcional(f"{canal}:COUPLING?")
    traza.impedancia = conexion.consultar_opcional(f"{canal}:IMPEDANCE?")
    # La sonda importa: con una sonda 10X la perilla muestra 10 veces más volts por división
    traza.atenuacion_sonda = conexion.consultar_numero_opcional(f"{canal}:PROBE?")

    # Paso 2: formato de transferencia. Texto (ASCII) es más lento que binario,
    # pero no se corrompe con ningún adaptador; 2 bytes por punto no pierde
    # resolución; del punto 1 al último para traer el registro completo.
    conexion.escribir(f"DATA:SOURCE {canal}")
    conexion.escribir("DATA:ENCDG ASCII")
    conexion.escribir(f"DATA:WIDTH {BYTES_POR_PUNTO}")
    conexion.escribir("DATA:START 1")
    conexion.escribir(f"DATA:STOP {longitud_registro}")

    # Paso 3: preámbulo. En modo Envelope los puntos vienen en pares
    # mínimo/máximo y las fórmulas de arriba no aplican.
    traza.preambulo = conexion.leer_preambulo(canal)
    formato_de_puntos = traza.preambulo.get("PT_FMT", "Y").upper()
    if formato_de_puntos.startswith("ENV"):
        return traza

    # Paso 4: todos los puntos
    texto_de_la_curva = conexion.leer_curva()
    traza.niveles = convertir_curva_a_niveles(texto_de_la_curva)

    # Paso 5: pasar a segundos, volts y divisiones
    calcular_unidades_de_la_traza(traza)
    return traza


def leer_osciloscopio(nombre_recurso, canales_pedidos, cola_de_mensajes):
    """
    Lee del osciloscopio la base de tiempo y los canales pedidos.

    Corre dentro de un hilo: no toca la ventana y avisa su avance poniendo
    tuplas ("progreso", texto) en la cola.

    Parámetros
    ----------
    nombre_recurso : str
        Dirección VISA del osciloscopio.
    canales_pedidos : lista de str
        Canales a leer, por ejemplo ["CH1", "CH3"].
    cola_de_mensajes : queue.Queue
        Cola donde se publica el avance.

    Retorna
    -------
    LecturaDelOsciloscopio

    Errores
    -------
    RuntimeError si no se pudo leer ningún canal.
    pyvisa.errors.VisaIOError si el osciloscopio deja de responder.
    """
    lectura = LecturaDelOsciloscopio(nombre_recurso)
    conexion = ConexionOsciloscopio(nombre_recurso)
    try:
        # Paso 1: identificar el instrumento
        lectura.identificacion = conexion.consultar_texto("*IDN?")
        if "TDS" not in lectura.identificacion.upper():
            lectura.avisos.append(f"El instrumento se identifica como «{lectura.identificacion}», "
                                  "no como un TDS. Revisa la dirección.")

        # Paso 2: base de tiempo y adquisición
        cola_de_mensajes.put(("progreso", "Leyendo la base de tiempo…"))
        lectura.horizontal = leer_ajustes_horizontales(conexion)
        if lectura.horizontal.esta_adquiriendo:
            lectura.avisos.append("El osciloscopio estaba en RUN: cada canal puede venir de un "
                                  "disparo distinto. Detenlo con RUN/STOP antes de leer.")

        # Paso 3: cada canal pedido que esté en pantalla
        for canal in canales_pedidos:
            if not canal_esta_en_pantalla(conexion, canal):
                lectura.avisos.append(f"{canal} no está encendido en la pantalla; no se leyó.")
                continue
            cola_de_mensajes.put(("progreso", f"Leyendo {canal} "
                                  f"({lectura.horizontal.longitud_registro} puntos)…"))
            traza = leer_canal(conexion, canal, lectura.horizontal.longitud_registro)
            if traza.niveles is None:
                lectura.avisos.append(f"{canal} está en modo Envelope (pares mínimo/máximo). "
                                      "Cámbialo a Sample o Average en ACQUIRE para leerlo.")
                continue
            lectura.canales.append(traza)
    finally:
        # Paso 4: cerrar la conexión pase lo que pase
        conexion.cerrar()

    if len(lectura.canales) == 0:
        detalle = " ".join(lectura.avisos)
        raise RuntimeError("No se leyó ningún canal. " + detalle)
    return lectura


def configurar_registro_maximo(nombre_recurso):
    """
    Pide al osciloscopio la longitud de registro máxima y confirma que la aceptó.

    Ojo: cambia un ajuste del osciloscopio. El registro nuevo se llena
    recién en el próximo disparo.

    Parámetros
    ----------
    nombre_recurso : str
        Dirección VISA del osciloscopio.

    Retorna
    -------
    int
        Longitud de registro que quedó configurada, en puntos.

    Errores
    -------
    RuntimeError si el osciloscopio no quedó con la longitud pedida.
    """
    conexion = ConexionOsciloscopio(nombre_recurso)
    try:
        conexion.escribir(f"HORIZONTAL:RECORDLENGTH {LONGITUD_MAXIMA_REGISTRO}")
        # Volvemos a preguntar para confirmar que el cambio se hizo
        longitud = int(round(conexion.consultar_numero("HORIZONTAL:RECORDLENGTH?")))
    finally:
        conexion.cerrar()
    if longitud != LONGITUD_MAXIMA_REGISTRO:
        raise RuntimeError(f"Se pidió un registro de {LONGITUD_MAXIMA_REGISTRO} puntos, pero el "
                           f"osciloscopio quedó en {longitud}.")
    return longitud


# ─── Funciones: verificación y ventana de pantalla ─────────────────────────

def elegir_prefijo(valor_de_referencia):
    """
    Elige el prefijo de unidad para un eje (por ejemplo n para nanosegundos).

    Se elige el prefijo más grande que deje el valor de referencia en 1 o más.
    Así 1e-7 s queda como 100 ns, y 0,9 V queda como 900 mV.

    Parámetros
    ----------
    valor_de_referencia : float
        Un valor típico del eje (la base de tiempo, el voltaje máximo, etc.),
        en unidades base (s o V).

    Retorna
    -------
    tupla (factor, prefijo)
        factor : float, por el que hay que dividir los valores del eje.
        prefijo : str, letra que va antes de la unidad en el título ("n", "µ", "m" o "").
        Si el valor es 0, retorna (1.0, "").
    """
    magnitud = abs(valor_de_referencia)
    if magnitud == 0:
        return 1.0, ""
    # Partimos con el prefijo más chico y subimos mientras el valor lo permita
    factor_elegido = PREFIJOS_DE_UNIDAD[0][0]
    prefijo_elegido = PREFIJOS_DE_UNIDAD[0][1]
    for opcion in PREFIJOS_DE_UNIDAD:
        factor = opcion[0]
        if magnitud >= factor:
            factor_elegido = factor
            prefijo_elegido = opcion[1]
    return factor_elegido, prefijo_elegido

def es_parecido(valor_a, valor_b, escala_de_referencia):
    """
    Dice si dos valores coinciden dentro de TOLERANCIA_DE_VERIFICACION.

    Parámetros
    ----------
    valor_a, valor_b : float
        Valores a comparar.
    escala_de_referencia : float
        Tamaño típico de los valores; la diferencia se mide como fracción de él.

    Retorna
    -------
    bool
    """
    if escala_de_referencia == 0:
        return valor_a == valor_b
    diferencia_relativa = abs(valor_a - valor_b) / abs(escala_de_referencia)
    return diferencia_relativa <= TOLERANCIA_DE_VERIFICACION


def verificar_canal(traza):
    """
    Compara lo que dicen los datos con lo que dicen las perillas del canal.

    Si todo calza, el gráfico reproduce la pantalla y los voltajes son los
    que el osciloscopio midió.

    Parámetros
    ----------
    traza : TrazaDeCanal

    Retorna
    -------
    lista de tuplas (bool, str)
        Cada verificación: si salió bien y un texto para el usuario.
    """
    resultados = []
    formato_V_por_div = EngFormatter(unit="V/div", places=4)
    formato_V = EngFormatter(unit="V", places=4)
    multiplicador_V = traza.valor_del_preambulo("YMULT", 0.0)
    offset_niveles = traza.valor_del_preambulo("YOFF", 0.0)
    cero_V = traza.valor_del_preambulo("YZERO", 0.0)

    # Verificación 1: escala vertical de los datos contra la perilla V/div
    escala_de_los_datos_V = multiplicador_V * traza.niveles_por_division
    coincide = es_parecido(escala_de_los_datos_V, traza.volts_por_division_V, traza.volts_por_division_V)
    texto = (f"{traza.nombre}: datos {formato_V_por_div(escala_de_los_datos_V)}, "
             f"perilla {formato_V_por_div(traza.volts_por_division_V)}")
    # Si no calzan, revisar si la diferencia es justo la atenuación de la sonda
    if not coincide and traza.atenuacion_sonda is not None and escala_de_los_datos_V != 0:
        razon_entre_escalas = traza.volts_por_division_V / escala_de_los_datos_V
        if es_parecido(razon_entre_escalas, traza.atenuacion_sonda, traza.atenuacion_sonda):
            texto = texto + f" (la diferencia es la sonda {traza.atenuacion_sonda:g}X)"
    resultados.append((coincide, texto))

    # Verificación 2: voltaje en el centro de la pantalla según los datos y según POSITION y OFFSET
    centro_segun_datos_V = cero_V + multiplicador_V * (0.0 - offset_niveles)
    centro_segun_perillas_V = traza.offset_V - traza.posicion_div * traza.volts_por_division_V
    coincide = es_parecido(centro_segun_datos_V, centro_segun_perillas_V, traza.volts_por_division_V)
    resultados.append((coincide, f"{traza.nombre}: centro de pantalla {formato_V(centro_segun_datos_V)} "
                                 f"(perillas: {formato_V(centro_segun_perillas_V)})"))

    # Verificación 3: llegaron todos los puntos anunciados
    if "NR_PT" in traza.preambulo:
        puntos_anunciados = int(traza.valor_del_preambulo("NR_PT", 0.0))
        coincide = puntos_anunciados == traza.numero_de_puntos()
        resultados.append((coincide, f"{traza.nombre}: {traza.numero_de_puntos()} puntos recibidos "
                                     f"de {puntos_anunciados} anunciados"))

    # Verificación 4: la señal no saturó el digitalizador
    altura_maxima_div = float(np.max(np.abs(traza.divisiones_en_pantalla)))
    if altura_maxima_div >= DIVISIONES_DE_SATURACION:
        resultados.append((False, f"{traza.nombre}: saturó el digitalizador (llegó a "
                                  f"{altura_maxima_div:.2f} div); esos tramos no son reales"))
    return resultados


def verificar_horizontal(lectura):
    """
    Revisa la base de tiempo con los datos del primer canal.

    Parámetros
    ----------
    lectura : LecturaDelOsciloscopio

    Retorna
    -------
    lista de tuplas (bool, str)
    """
    resultados = []
    primera = lectura.canales[0]
    ajustes = lectura.horizontal
    intervalo_de_muestreo_s = primera.valor_del_preambulo("XINCR", 0.0)
    indice_del_trigger = primera.valor_del_preambulo("PT_OFF", 0.0)

    # Verificación 1: el trigger está donde dice la perilla de posición del trigger
    if ajustes.posicion_trigger_porcentaje is not None:
        indice_esperado = ajustes.posicion_trigger_porcentaje / 100.0 * primera.numero_de_puntos()
        # Se acepta 1 % del registro o 2 puntos, lo que sea mayor
        margen_en_puntos = max(2.0, TOLERANCIA_DE_VERIFICACION * primera.numero_de_puntos())
        coincide = abs(indice_esperado - indice_del_trigger) <= margen_en_puntos
        resultados.append((coincide, f"Trigger en el punto {indice_del_trigger:.0f} "
                                     f"({ajustes.posicion_trigger_porcentaje:g} % del registro)"))

    # Verificación 2: todos los canales comparten la misma base de tiempo
    for traza in lectura.canales:
        if traza is primera:
            continue
        mismos_puntos = traza.numero_de_puntos() == primera.numero_de_puntos()
        mismo_intervalo = es_parecido(traza.valor_del_preambulo("XINCR", 0.0), intervalo_de_muestreo_s,
                                      intervalo_de_muestreo_s)
        mismo_trigger = traza.valor_del_preambulo("PT_OFF", 0.0) == indice_del_trigger
        if not (mismos_puntos and mismo_intervalo and mismo_trigger):
            resultados.append((False, f"{traza.nombre} tiene otra base de tiempo que {primera.nombre}"))
    return resultados


def calcular_ventana_de_pantalla(lectura):
    """
    Calcula qué tramo de tiempo se ve en la pantalla del osciloscopio.

    La pantalla mide 10 divisiones × (s/div). Si el registro completo cabe
    en ella (registro de 500 puntos o "Fit to Screen"), la pantalla empieza
    en el primer punto y el tramo es exacto. Si el registro es más largo, la
    pantalla muestra solo una parte, elegida con la perilla de posición
    horizontal (porcentaje del registro a la izquierda del centro).

    Parámetros
    ----------
    lectura : LecturaDelOsciloscopio

    Retorna
    -------
    tupla (inicio_s, fin_s, es_exacta)
        inicio_s, fin_s : float, bordes izquierdo y derecho de la pantalla en s.
        es_exacta : bool, False si se estimó con la posición horizontal.
    """
    primera = lectura.canales[0]
    duracion_pantalla_s = DIVISIONES_HORIZONTALES * lectura.horizontal.segundos_por_division_s
    inicio_registro_s = float(primera.tiempos_s[0])
    fin_registro_s = float(primera.tiempos_s[-1])
    duracion_registro_s = fin_registro_s - inicio_registro_s

    # Caso 1: el registro entero cabe en la pantalla (con un pequeño margen numérico)
    if duracion_registro_s <= duracion_pantalla_s * (1.0 + TOLERANCIA_DE_VERIFICACION):
        return inicio_registro_s, inicio_registro_s + duracion_pantalla_s, True

    # Caso 2: la pantalla muestra una parte; el centro lo fija la posición horizontal
    posicion_porcentaje = lectura.horizontal.posicion_horizontal_porcentaje
    if posicion_porcentaje is None:
        posicion_porcentaje = 50.0
    centro_s = inicio_registro_s + posicion_porcentaje / 100.0 * duracion_registro_s
    inicio_s = centro_s - duracion_pantalla_s / 2.0
    # No dejar que la ventana se salga del registro
    if inicio_s < inicio_registro_s:
        inicio_s = inicio_registro_s
    if inicio_s + duracion_pantalla_s > fin_registro_s:
        inicio_s = fin_registro_s - duracion_pantalla_s
    return inicio_s, inicio_s + duracion_pantalla_s, False


# ─── Funciones: datos del disparo ──────────────────────────────────────────

def texto_o_sin_dato(texto):
    """
    Parámetros
    ----------
    texto : str

    Retorna
    -------
    str
        El mismo texto, o "sin dato" si viene vacío.
    """
    if texto == "":
        return "sin dato"
    return texto


def numero_o_sin_dato(valor):
    """
    Parámetros
    ----------
    valor : float o None

    Retorna
    -------
    str
        El número como texto, o "sin dato" si es None.
    """
    if valor is None:
        return "sin dato"
    return f"{valor:g}"


def convertir_texto_opcional_a_numero(texto, nombre_del_campo):
    """
    Convierte el texto de un campo en número, aceptando coma decimal.

    Parámetros
    ----------
    texto : str
        Lo que escribió el usuario, por ejemplo "25" o "1,5".
    nombre_del_campo : str
        Nombre del campo, para el mensaje de error.

    Retorna
    -------
    float o None
        El número, o None si el campo quedó vacío.

    Errores
    -------
    ValueError con un mensaje para el usuario si no es un número.
    """
    limpio = texto.strip().replace(",", ".")
    if limpio == "":
        return None
    try:
        return float(limpio)
    except ValueError:
        raise ValueError(f"«{nombre_del_campo}» debe ser un número, por ejemplo 25 o 1,5. "
                         f"Se escribió «{texto}».")


def limpiar_para_nombre_de_archivo(texto):
    """
    Deja solo letras, números, guiones y guiones bajos, para usar el texto en
    el nombre de un archivo (Windows no acepta caracteres como / : * ?).

    Parámetros
    ----------
    texto : str

    Retorna
    -------
    str
        Por ejemplo "123" para "#123", o "D2_Ar" para "D2/Ar".
    """
    limpio = ""
    for caracter in texto.strip():
        if caracter.isalnum() or caracter == "-" or caracter == "_":
            limpio = limpio + caracter
        elif caracter == " " or caracter == "/":
            limpio = limpio + "_"
    return limpio


def sugerir_siguiente_disparo(numero_anterior):
    """
    Propone el número del próximo disparo a partir del anterior.

    Parámetros
    ----------
    numero_anterior : str
        Número del disparo guardado antes, por ejemplo "123".

    Retorna
    -------
    str
        El siguiente número ("124") si el anterior era un entero; si no,
        el mismo texto, para que el usuario lo corrija.
    """
    limpio = numero_anterior.strip()
    if limpio.isdigit():
        siguiente = int(limpio) + 1
        return str(siguiente)
    return limpio


# ─── Funciones: archivo .csv ───────────────────────────────────────────────

def armar_encabezado(lectura, verificaciones, ventana, datos_del_disparo):
    """
    Arma las líneas de metadatos del .csv.

    Parámetros
    ----------
    lectura : LecturaDelOsciloscopio
    verificaciones : lista de tuplas (bool, str)
        Resultados de verificar_canal y verificar_horizontal.
    ventana : tupla (inicio_s, fin_s, es_exacta)
        Tramo visible en la pantalla, de calcular_ventana_de_pantalla.
    datos_del_disparo : DatosDelDisparo
        Número de disparo, diferencia de potencial, distancia y gas.

    Retorna
    -------
    lista de str
        Líneas de texto sin el "# " inicial.
    """
    ajustes = lectura.horizontal
    primera = lectura.canales[0]
    lineas = []

    # Paso 1: datos del experimento, primero porque es lo que más se busca
    lineas.append("Lectura por GPIB del osciloscopio Tektronix TDS 684A")
    for linea in datos_del_disparo.lineas_de_encabezado():
        lineas.append(linea)

    # Paso 2: datos generales del osciloscopio
    lineas.append("instrumento: " + lectura.identificacion)
    lineas.append("recurso_visa: " + lectura.recurso)
    lineas.append("fecha: " + lectura.fecha)
    lineas.append(f"base_de_tiempo_s_por_div: {ajustes.segundos_por_division_s:.6e}")
    lineas.append(f"longitud_registro_puntos: {ajustes.longitud_registro}")
    lineas.append(f"intervalo_de_muestreo_s: {primera.valor_del_preambulo('XINCR', 0.0):.6e}")
    lineas.append(f"posicion_trigger_porcentaje: {ajustes.posicion_trigger_porcentaje}")
    lineas.append(f"posicion_horizontal_porcentaje: {ajustes.posicion_horizontal_porcentaje}")
    lineas.append(f"modo_adquisicion: {ajustes.modo_adquisicion}  promedios: {ajustes.numero_de_promedios}")
    tipo_de_ventana = "exacta"
    if not ventana[2]:
        tipo_de_ventana = "estimada con la posicion horizontal"
    lineas.append(f"pantalla_visible_s: {ventana[0]:.6e} a {ventana[1]:.6e} ({tipo_de_ventana})")

    # Paso 3: escalas y preámbulo de cada canal
    for traza in lectura.canales:
        lineas.append(f"{traza.nombre}: escala_V_por_div={traza.volts_por_division_V:.6e} "
                      f"posicion_div={traza.posicion_div:.4f} offset_V={traza.offset_V:.6e} "
                      f"acoplamiento={traza.acoplamiento} impedancia={traza.impedancia} "
                      f"sonda={traza.atenuacion_sonda}")
        texto_preambulo = ""
        for clave in ["NR_PT", "XINCR", "PT_OFF", "XZERO", "YMULT", "YOFF", "YZERO", "BYT_NR"]:
            if clave in traza.preambulo:
                texto_preambulo = texto_preambulo + f"{clave}={traza.preambulo[clave]} "
        lineas.append(f"{traza.nombre}_preambulo: {texto_preambulo.strip()}")
        if "WFID" in traza.preambulo:
            lineas.append(f"{traza.nombre}_descripcion: {traza.preambulo['WFID']}")

    # Paso 4: fórmulas y verificaciones
    lineas.append("tiempo_s = XZERO + XINCR*(indice - PT_OFF); voltaje_V = YZERO + YMULT*(nivel - YOFF)")
    for verificacion in verificaciones:
        resultado = "OK"
        if not verificacion[0]:
            resultado = "REVISAR"
        lineas.append(f"verificacion {resultado}: {verificacion[1]}")
    for aviso in lectura.avisos:
        lineas.append("aviso: " + aviso)
    return lineas


def escribir_csv(ruta_csv, lectura, lineas_de_encabezado):
    """
    Escribe el .csv: metadatos, nombres de columnas y una fila por punto.

    Todos los canales comparten la columna de tiempo; quien llama debe haber
    revisado antes que tengan la misma base de tiempo.

    Parámetros
    ----------
    ruta_csv : str
        Ruta del archivo a crear (se sobrescribe si existe).
    lectura : LecturaDelOsciloscopio
    lineas_de_encabezado : lista de str
        Metadatos; cada línea se escribe precedida de "# ".

    Errores
    -------
    OSError si no se puede escribir (por ejemplo, el archivo está abierto en Excel).
    """
    primera = lectura.canales[0]
    with open(ruta_csv, "w", encoding="utf-8", newline="") as archivo:
        # Paso 1: metadatos como comentarios, que pandas y numpy saben saltar
        for linea in lineas_de_encabezado:
            archivo.write("# " + linea + "\n")

        # Paso 2: nombres de las columnas
        nombres = "indice,tiempo_s"
        for traza in lectura.canales:
            nombres = nombres + f",{traza.nombre}_V"
        archivo.write(nombres + "\n")

        # Paso 3: una fila por punto del registro
        for i in range(primera.numero_de_puntos()):
            fila = f"{i},{primera.tiempos_s[i]:.9e}"
            for traza in lectura.canales:
                fila = fila + f",{traza.voltajes_V[i]:.7e}"
            archivo.write(fila + "\n")


# ─── Estilo de la interfaz (de la plantilla de interfaces) ─────────────────

def _pick_font(root, preferred, fallback="TkDefaultFont"):
    """
    Devuelve la primera tipografía de la lista que esté instalada.

    Parámetros
    ----------
    root : tk.Tk
        Ventana principal (se necesita para consultar las tipografías).
    preferred : lista de str
        Nombres de tipografías en orden de preferencia.
    fallback : str
        Tipografía a usar si ninguna está instalada.

    Retorna
    -------
    str
    """
    available = set(font.families(root))
    for fam in preferred:
        if fam in available:
            return fam
    return fallback


def setup_style(root):
    """
    Configura el tema visual de ttk con los tokens de diseño.

    Parámetros
    ----------
    root : tk.Tk
        Ventana principal.

    Retorna
    -------
    dict
        Tipografías por uso ("title", "heading", "body", "muted", "readout", "mono").
    """
    style = ttk.Style(root)
    style.theme_use("clam")

    # Paso 1: elegir tipografías disponibles
    ui = _pick_font(root, ["Segoe UI", "SF Pro Text", "Helvetica Neue", "DejaVu Sans"])
    mono = _pick_font(root, ["Cascadia Code", "Consolas", "Menlo", "DejaVu Sans Mono"])
    FONTS = {
        "title":   (ui, 17, "bold"),
        "heading": (ui, 12, "bold"),
        "body":    (ui, 10),
        "muted":   (ui, 9),
        "readout": (mono, 24, "bold"),
        "mono":    (mono, 9),
    }

    # Paso 2: estilo general
    root.configure(bg=COLORS["bg"])
    style.configure(".", background=COLORS["bg"], foreground=COLORS["text"],
                    font=FONTS["body"], focuscolor=COLORS["accent"])

    # Paso 3: contenedores
    style.configure("App.TFrame", background=COLORS["bg"])
    style.configure("Card.TFrame", background=COLORS["surface"],
                    bordercolor=COLORS["border"], borderwidth=1, relief="solid")

    # Paso 4: etiquetas
    style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"])
    style.configure("Title.TLabel", font=FONTS["title"])
    style.configure("Heading.TLabel", font=FONTS["heading"])
    style.configure("Muted.TLabel", foreground=COLORS["muted"], font=FONTS["muted"])
    style.configure("Card.TLabel", background=COLORS["surface"])
    style.configure("CardMuted.TLabel", background=COLORS["surface"],
                    foreground=COLORS["muted"], font=FONTS["muted"])
    style.configure("Readout.TLabel", background=COLORS["surface"],
                    foreground=COLORS["text"], font=FONTS["readout"])
    # Agregados para este programa: título de sección y lecturas dentro de una tarjeta
    style.configure("CardHeading.TLabel", background=COLORS["surface"], font=FONTS["heading"])
    style.configure("CardMono.TLabel", background=COLORS["surface"],
                    foreground=COLORS["text"], font=FONTS["mono"])
    style.configure("CardMonoWarn.TLabel", background=COLORS["surface"],
                    foreground=COLORS["warn"], font=FONTS["mono"])
    style.configure("Card.TCheckbutton", background=COLORS["surface"])
    style.configure("Card.TRadiobutton", background=COLORS["surface"])

    # Paso 5: botón neutro
    style.configure("TButton", font=FONTS["body"], padding=(PAD_L, PAD_M),
                    background=COLORS["surface"], foreground=COLORS["text"],
                    bordercolor=COLORS["border"], relief="solid", borderwidth=1)
    style.map("TButton", background=[("active", "#EEF1F5"), ("pressed", "#E4E9EF")])

    # Paso 6: botón de acción principal (un solo uso por vista)
    style.configure("Accent.TButton", background=COLORS["accent"],
                    foreground=COLORS["accent_fg"], bordercolor=COLORS["accent"])
    style.map("Accent.TButton",
              background=[("active", "#26538F"), ("pressed", "#1F4576"),
                          ("disabled", "#AEBACB")])

    # Paso 7: campos de texto
    style.configure("TEntry", fieldbackground=COLORS["surface"],
                    bordercolor=COLORS["border"], padding=PAD_S)
    style.configure("TCombobox", fieldbackground=COLORS["surface"],
                    bordercolor=COLORS["border"], padding=PAD_S)
    return FONTS


def estilizar_ejes(ejes):
    """
    Da a un gráfico de matplotlib el estilo de la interfaz.

    Parámetros
    ----------
    ejes : matplotlib.axes.Axes
    """
    ejes.set_facecolor(COLORS["surface"])
    # Menos líneas alrededor del gráfico, para que destaque la señal
    ejes.spines["top"].set_visible(False)
    ejes.spines["right"].set_visible(False)
    ejes.spines["left"].set_color(COLORS["border"])
    ejes.spines["bottom"].set_color(COLORS["border"])
    ejes.tick_params(colors=COLORS["muted"], labelsize=8)
    ejes.grid(True, color=COLORS["border"], linewidth=0.6, alpha=0.7)
    ejes.xaxis.label.set_color(COLORS["muted"])
    ejes.yaxis.label.set_color(COLORS["muted"])


# ─── Ventana de datos del disparo ──────────────────────────────────────────

class DialogoDatosDelDisparo:
    """
    Ventana pequeña que pregunta los datos del disparo antes de guardar.

    La crea App al presionar «Guardar CSV…». Mientras está abierta no se
    puede usar la ventana principal (es "modal"). Se cierra con «Continuar»
    (o Enter) o con «Cancelar» (o Escape).

    Atributos
    ---------
    ventana : tk.Toplevel
        La ventana del diálogo.
    texto_disparo, texto_potencial, texto_distancia, texto_gas : tk.StringVar
        Lo escrito en cada campo.
    resultado : DatosDelDisparo o None
        Los datos aceptados, o None si se canceló.
    """

    def __init__(self, ventana_padre, datos_iniciales):
        """
        Crea el diálogo con los campos ya llenos con los datos iniciales.

        Parámetros
        ----------
        ventana_padre : tk.Tk o tk.Widget
            Ventana principal, sobre la que aparece el diálogo.
        datos_iniciales : DatosDelDisparo
            Valores con que parten los campos (los del disparo anterior,
            con el número ya avanzado en uno).
        """
        self.resultado = None

        # Paso 1: ventana encima de la principal
        self.ventana = tk.Toplevel(ventana_padre)
        self.ventana.title("Datos del disparo")
        self.ventana.configure(bg=COLORS["bg"])
        self.ventana.transient(ventana_padre)
        self.ventana.resizable(False, False)
        self.ventana.protocol("WM_DELETE_WINDOW", self.al_cancelar)

        # Paso 2: tarjeta con los campos
        tarjeta = ttk.Frame(self.ventana, style="Card.TFrame", padding=PAD_L)
        tarjeta.grid(row=0, column=0, padx=PAD_L, pady=PAD_L, sticky="nsew")
        titulo = ttk.Label(tarjeta, text="Datos del disparo", style="CardHeading.TLabel")
        titulo.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, PAD_M))

        self.texto_disparo = tk.StringVar(value=datos_iniciales.numero_de_disparo)
        self.texto_potencial = tk.StringVar(value=numero_o_vacio(datos_iniciales.diferencia_de_potencial_kV))
        self.texto_distancia = tk.StringVar(value=numero_o_vacio(datos_iniciales.distancia_detector_cm))
        self.texto_gas = tk.StringVar(value=datos_iniciales.gas)

        self.campo_disparo = self.crear_campo(tarjeta, 1, "Número de disparo", self.texto_disparo, "")
        self.crear_campo(tarjeta, 2, "Diferencia de potencial", self.texto_potencial, "kV")
        self.crear_campo(tarjeta, 3, "Distancia del detector", self.texto_distancia, "cm")
        etiqueta_gas = ttk.Label(tarjeta, text="Gas usado", style="Card.TLabel")
        etiqueta_gas.grid(row=4, column=0, sticky="w", padx=(0, PAD_M), pady=PAD_S)
        # Lista desplegable que también permite escribir otro gas
        lista_de_gases = ttk.Combobox(tarjeta, textvariable=self.texto_gas, values=GASES_FRECUENTES, width=12)
        lista_de_gases.grid(row=4, column=1, sticky="w", pady=PAD_S)

        nota = ttk.Label(tarjeta, style="CardMuted.TLabel",
                         text="Los campos vacíos se guardan como «sin dato».")
        nota.grid(row=5, column=0, columnspan=3, sticky="w", pady=(PAD_S, 0))

        # Paso 3: botones (Continuar es la acción principal)
        botones = ttk.Frame(tarjeta, style="Card.TFrame", borderwidth=0)
        botones.grid(row=6, column=0, columnspan=3, sticky="e", pady=(PAD_L, 0))
        boton_cancelar = ttk.Button(botones, text="Cancelar", command=self.al_cancelar)
        boton_cancelar.grid(row=0, column=0, padx=(0, PAD_M))
        boton_continuar = ttk.Button(botones, text="Continuar", style="Accent.TButton", command=self.al_aceptar)
        boton_continuar.grid(row=0, column=1)

        # Paso 4: atajos de teclado
        self.ventana.bind("<Return>", self.al_aceptar)
        self.ventana.bind("<Escape>", self.al_cancelar)

    def crear_campo(self, contenedor, fila, texto_etiqueta, variable, unidad):
        """
        Pone una etiqueta, un campo de texto y su unidad en una fila.

        Parámetros
        ----------
        contenedor : ttk.Frame
        fila : int
        texto_etiqueta : str
        variable : tk.StringVar
            Variable que guarda lo escrito.
        unidad : str
            Unidad que se muestra a la derecha ("" si no tiene).

        Retorna
        -------
        ttk.Entry
            El campo creado.
        """
        etiqueta = ttk.Label(contenedor, text=texto_etiqueta, style="Card.TLabel")
        etiqueta.grid(row=fila, column=0, sticky="w", padx=(0, PAD_M), pady=PAD_S)
        campo = ttk.Entry(contenedor, textvariable=variable, width=14)
        campo.grid(row=fila, column=1, sticky="w", pady=PAD_S)
        etiqueta_unidad = ttk.Label(contenedor, text=unidad, style="CardMuted.TLabel")
        etiqueta_unidad.grid(row=fila, column=2, sticky="w", padx=(PAD_S, 0))
        return campo

    def mostrar(self):
        """
        Muestra el diálogo y espera a que el usuario lo cierre.

        Retorna
        -------
        DatosDelDisparo o None
            Los datos escritos, o None si se canceló.
        """
        # Centrar sobre la ventana principal para que aparezca a la vista
        self.centrar_sobre_la_ventana_principal()
        # grab_set bloquea la ventana principal mientras el diálogo está abierto
        self.ventana.grab_set()
        self.campo_disparo.focus_set()
        self.campo_disparo.select_range(0, "end")
        # wait_window detiene esta función hasta que el diálogo se cierre
        self.ventana.wait_window()
        return self.resultado

    def centrar_sobre_la_ventana_principal(self):
        """Ubica el diálogo en el centro de la ventana principal."""
        # Paso 1: que tkinter calcule el tamaño real de ambas ventanas
        self.ventana.update_idletasks()
        principal = self.ventana.master
        # Paso 2: centro de la ventana principal en la pantalla, en píxeles
        centro_x = principal.winfo_rootx() + principal.winfo_width() // 2
        centro_y = principal.winfo_rooty() + principal.winfo_height() // 2
        # Paso 3: esquina superior izquierda del diálogo para que quede centrado
        x = centro_x - self.ventana.winfo_reqwidth() // 2
        y = centro_y - self.ventana.winfo_reqheight() // 2
        self.ventana.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def al_aceptar(self, evento=None):
        """
        Botón «Continuar» o tecla Enter: revisa los números y cierra el diálogo.

        Parámetros
        ----------
        evento : tk.Event o None
            Lo entrega tkinter cuando se usa la tecla Enter; no se usa.
        """
        try:
            potencial_kV = convertir_texto_opcional_a_numero(self.texto_potencial.get(), "Diferencia de potencial")
            distancia_cm = convertir_texto_opcional_a_numero(self.texto_distancia.get(), "Distancia del detector")
        except ValueError as error:
            messagebox.showerror("Dato no válido", str(error), parent=self.ventana)
            return
        self.resultado = DatosDelDisparo(self.texto_disparo.get().strip(), potencial_kV,
                                         distancia_cm, self.texto_gas.get().strip())
        self.ventana.destroy()

    def al_cancelar(self, evento=None):
        """
        Botón «Cancelar», tecla Escape o la X: cierra sin guardar.

        Parámetros
        ----------
        evento : tk.Event o None
            Lo entrega tkinter cuando se usa la tecla Escape; no se usa.
        """
        self.resultado = None
        self.ventana.destroy()


def numero_o_vacio(valor):
    """
    Parámetros
    ----------
    valor : float o None

    Retorna
    -------
    str
        El número como texto para llenar un campo, o "" si es None.
    """
    if valor is None:
        return ""
    return f"{valor:g}"


# ─── Ventana principal ─────────────────────────────────────────────────────

class App(ttk.Frame):
    """
    Ventana principal: conecta con el osciloscopio, muestra las escalas
    leídas y las verificaciones, grafica y guarda el .csv.

    La crea main(). Hereda de ttk.Frame (ver encabezado del archivo).

    Atributos principales
    ---------------------
    escala_pantalla : float
        Factor de tamaño según la resolución de la pantalla (1.0 = normal).
    lectura : LecturaDelOsciloscopio o None
        Última lectura completa.
    verificaciones : lista de tuplas (bool, str)
        Resultados de las verificaciones de la última lectura.
    cola_de_resultados : queue.Queue
        Por aquí los hilos entregan sus resultados a la ventana.
    hay_tarea_en_curso : bool
        True mientras un hilo habla con el osciloscopio.
    id_revision_pendiente : str o None
        Identificador del próximo after() que revisa la cola.
    ultimos_datos_del_disparo : DatosDelDisparo o None
        Datos escritos al guardar el disparo anterior.
    dialogo_abierto : DialogoDatosDelDisparo o None
        El diálogo de datos mientras está en pantalla.
    """

    def __init__(self, master, escala_pantalla):
        """
        Crea la ventana con todos sus elementos.

        Parámetros
        ----------
        master : tk.Tk
            Ventana raíz de tkinter.
        escala_pantalla : float
            Factor de tamaño que entrega tune_scaling().
        """
        # Inicializa la parte de ttk.Frame (contenedor con margen y estilo)
        super().__init__(master, padding=PAD_XL, style="App.TFrame")
        self.escala_pantalla = escala_pantalla

        # Paso 1: estado
        self.lectura = None
        self.verificaciones = []
        self.cola_de_resultados = queue.Queue()
        self.hay_tarea_en_curso = False
        self.id_revision_pendiente = None
        # Datos del último disparo guardado, para proponerlos en el siguiente
        self.ultimos_datos_del_disparo = None
        # Diálogo de datos del disparo mientras está abierto (lo usan las pruebas)
        self.dialogo_abierto = None

        # Paso 2: ubicar el contenedor para que crezca con la ventana
        self.grid(row=0, column=0, sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # Paso 3: construir y mostrar el estado inicial
        self._build()
        self.dibujar_grafico()
        self.set_status("Sin conectar. Busca el osciloscopio o escribe su dirección VISA.", "idle")

    # ── Construcción de la ventana ────────────────────────────────────────

    def _build(self):
        """Crea el título, el gráfico, el panel de controles y la barra de estado."""
        titulo = ttk.Label(self, text="Osciloscopio TDS 684A · GPIB", style="Title.TLabel")
        titulo.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, PAD_L))
        self._build_grafico()
        self._build_panel_de_control()
        self._build_statusbar()

    def _build_grafico(self):
        """Crea la tarjeta con el gráfico y su barra de zoom."""
        # Paso 1: tarjeta que crece con la ventana
        tarjeta = ttk.Frame(self, style="Card.TFrame", padding=PAD_M)
        tarjeta.grid(row=1, column=0, sticky="nsew", padx=(0, PAD_L))
        tarjeta.columnconfigure(0, weight=1)
        tarjeta.rowconfigure(0, weight=1)

        # Paso 2: figura con un solo gráfico
        puntos_por_pulgada = round(100 * self.escala_pantalla)
        self.figura = Figure(figsize=(7.2, 5.4), dpi=puntos_por_pulgada)
        self.figura.set_facecolor(COLORS["surface"])
        self.ejes = self.figura.add_subplot(1, 1, 1)
        self.canvas = FigureCanvasTkAgg(self.figura, master=tarjeta)
        self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")

        # Paso 3: barra de zoom, para recorrer el registro completo punto a punto
        self.barra_de_herramientas = NavigationToolbar2Tk(self.canvas, tarjeta, pack_toolbar=False)
        self.barra_de_herramientas.grid(row=1, column=0, sticky="ew")
        # La barra viene gris; le damos el fondo blanco de la tarjeta a ella y a sus botones
        self.barra_de_herramientas.config(background=COLORS["surface"])
        for elemento in self.barra_de_herramientas.winfo_children():
            try:
                elemento.config(background=COLORS["surface"])
            except tk.TclError:
                # Algunos elementos de la barra no aceptan cambiar el fondo; se dejan como están
                pass

    def _build_panel_de_control(self):
        """Crea la tarjeta de la derecha con los pasos."""
        self.panel = ttk.Frame(self, style="Card.TFrame", padding=PAD_L)
        self.panel.grid(row=1, column=1, sticky="ns")
        self.panel.columnconfigure(0, weight=1)
        self.panel.columnconfigure(1, weight=1)
        # Cada sección recibe la fila donde empieza y devuelve la siguiente libre
        fila = 0
        fila = self._build_seccion_conexion(fila)
        fila = self._build_seccion_canales(fila)
        fila = self._build_seccion_lectura(fila)
        self._build_seccion_resultado(fila)

    def crear_encabezado(self, fila, texto):
        """
        Pone el título de una sección del panel.

        Parámetros
        ----------
        fila : int
            Fila de la grilla del panel.
        texto : str
            Título de la sección.

        Retorna
        -------
        int
            Siguiente fila libre.
        """
        # La primera sección no lleva espacio arriba
        espacio_arriba = PAD_L
        if fila == 0:
            espacio_arriba = 0
        etiqueta = ttk.Label(self.panel, text=texto, style="CardHeading.TLabel")
        etiqueta.grid(row=fila, column=0, columnspan=2, sticky="w", pady=(espacio_arriba, PAD_S))
        return fila + 1

    def crear_boton(self, fila, columna, texto, accion, estilo):
        """
        Pone un botón en el panel.

        Parámetros
        ----------
        fila, columna : int
            Posición en la grilla.
        texto : str
            Texto del botón.
        accion : método
            Método que se ejecuta al presionar el botón.
        estilo : str
            "TButton" para un botón neutro o "Accent.TButton" para el principal.

        Retorna
        -------
        ttk.Button
        """
        boton = ttk.Button(self.panel, text=texto, command=accion, style=estilo)
        boton.grid(row=fila, column=columna, sticky="ew", padx=(0, PAD_M), pady=PAD_S)
        return boton

    def _build_seccion_conexion(self, fila):
        """Sección 1: dirección VISA y búsqueda. Retorna la siguiente fila."""
        fila = self.crear_encabezado(fila, "1 · Conexión")
        self.texto_recurso = tk.StringVar(value=RECURSO_POR_DEFECTO)
        self.lista_de_recursos = ttk.Combobox(self.panel, textvariable=self.texto_recurso, width=22)
        self.lista_de_recursos.grid(row=fila, column=0, sticky="ew", padx=(0, PAD_M), pady=PAD_S)
        self.boton_buscar = self.crear_boton(fila, 1, "Buscar", self.al_buscar, "TButton")
        fila = fila + 1
        self.etiqueta_identificacion = ttk.Label(self.panel, text="", style="CardMuted.TLabel",
                                                 wraplength=int(300 * self.escala_pantalla))
        self.etiqueta_identificacion.grid(row=fila, column=0, columnspan=2, sticky="w")
        return fila + 1

    def _build_seccion_canales(self, fila):
        """Sección 2: canales a leer. Retorna la siguiente fila."""
        fila = self.crear_encabezado(fila, "2 · Canales")
        # Todos marcados: se leen los que estén encendidos en la pantalla
        self.canal_marcado = {}
        contenedor = ttk.Frame(self.panel, style="Card.TFrame", borderwidth=0)
        contenedor.grid(row=fila, column=0, columnspan=2, sticky="w")
        for i in range(len(CANALES)):
            canal = CANALES[i]
            variable = tk.BooleanVar(value=True)
            casilla = ttk.Checkbutton(contenedor, text=canal, variable=variable, style="Card.TCheckbutton")
            casilla.grid(row=0, column=i, sticky="w", padx=(0, PAD_M))
            self.canal_marcado[canal] = variable
        fila = fila + 1
        nota = ttk.Label(self.panel, style="CardMuted.TLabel", justify="left",
                         wraplength=int(300 * self.escala_pantalla),
                         text="Se leen los canales marcados que estén encendidos en la pantalla.")
        nota.grid(row=fila, column=0, columnspan=2, sticky="w")
        return fila + 1

    def _build_seccion_lectura(self, fila):
        """Sección 3: leer, registro máximo y guardar. Retorna la siguiente fila."""
        fila = self.crear_encabezado(fila, "3 · Lectura")
        # Única acción de acento: es lo que se hace cada vez
        self.boton_leer = self.crear_boton(fila, 0, "Leer osciloscopio", self.al_leer, "Accent.TButton")
        self.boton_guardar = self.crear_boton(fila, 1, "Guardar CSV…", self.al_guardar_csv, "TButton")
        fila = fila + 1
        self.boton_registro = self.crear_boton(fila, 0, f"Registro de {LONGITUD_MAXIMA_REGISTRO} pts",
                                               self.al_configurar_registro_maximo, "TButton")
        nota = ttk.Label(self.panel, style="CardMuted.TLabel", justify="left",
                         wraplength=int(150 * self.escala_pantalla),
                         text="Cambia el osciloscopio. Vale desde el próximo disparo.")
        nota.grid(row=fila, column=1, sticky="w")
        return fila + 1

    def _build_seccion_resultado(self, fila):
        """Sección 4: vista del gráfico, escalas leídas y verificaciones."""
        fila = self.crear_encabezado(fila, "4 · Resultado")

        # Paso 1: elegir qué mostrar
        self.vista = tk.StringVar(value="pantalla")
        opcion_pantalla = ttk.Radiobutton(self.panel, text="Pantalla del osciloscopio", value="pantalla",
                                          variable=self.vista, command=self.dibujar_grafico,
                                          style="Card.TRadiobutton")
        opcion_pantalla.grid(row=fila, column=0, columnspan=2, sticky="w")
        fila = fila + 1
        opcion_registro = ttk.Radiobutton(self.panel, text="Registro completo en volts", value="registro",
                                          variable=self.vista, command=self.dibujar_grafico,
                                          style="Card.TRadiobutton")
        opcion_registro.grid(row=fila, column=0, columnspan=2, sticky="w", pady=(0, PAD_M))
        fila = fila + 1

        # Paso 2: escalas leídas (letra monoespaciada para que se alineen)
        self.etiqueta_escalas = ttk.Label(self.panel, text="Escalas: sin leer", style="CardMono.TLabel",
                                          justify="left")
        self.etiqueta_escalas.grid(row=fila, column=0, columnspan=2, sticky="w", pady=(0, PAD_M))
        fila = fila + 1

        # Paso 3: verificaciones correctas (color normal) y avisos (color de advertencia)
        self.etiqueta_verificaciones = ttk.Label(self.panel, text="", style="CardMono.TLabel",
                                                 justify="left", wraplength=int(320 * self.escala_pantalla))
        self.etiqueta_verificaciones.grid(row=fila, column=0, columnspan=2, sticky="w")
        fila = fila + 1
        self.etiqueta_avisos = ttk.Label(self.panel, text="", style="CardMonoWarn.TLabel",
                                         justify="left", wraplength=int(320 * self.escala_pantalla))
        self.etiqueta_avisos.grid(row=fila, column=0, columnspan=2, sticky="w", pady=(PAD_S, 0))

    def _build_statusbar(self):
        """Crea la barra de estado inferior con un punto de color."""
        barra = ttk.Frame(self, style="App.TFrame")
        barra.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(PAD_L, 0))
        self._dot = tk.Label(barra, text="●", bg=COLORS["bg"],
                             fg=COLORS["muted"], font=("TkDefaultFont", 10))
        self._dot.grid(row=0, column=0, padx=(0, PAD_S))
        self._status = ttk.Label(barra, text="", style="Muted.TLabel")
        self._status.grid(row=0, column=1, sticky="w")

    def set_status(self, text, state="idle"):
        """
        Actualiza la barra de estado.

        Parámetros
        ----------
        text : str
            Mensaje a mostrar.
        state : str
            "ok" (verde), "warn" (amarillo), "error" (rojo) o "idle" (gris).
        """
        color_del_punto = COLORS["muted"]
        if state in COLORS:
            color_del_punto = COLORS[state]
        self._dot.config(fg=color_del_punto)
        self._status.config(text=text)

    # ── Tareas en hilos ────────────────────────────────────────────────────

    def puede_empezar_tarea(self):
        """
        Revisa que no haya otra tarea en curso y que PyVISA esté instalado.

        Retorna
        -------
        bool
            True si se puede empezar. Si no, avisa al usuario.
        """
        if self.hay_tarea_en_curso:
            return False
        if pyvisa is None:
            messagebox.showerror("Falta PyVISA", "Para conectarse hay que instalar PyVISA:\n\npip install pyvisa")
            return False
        return True

    def empezar_tarea(self, funcion_del_hilo, argumentos, mensaje):
        """
        Lanza una tarea en un hilo y empieza a revisar la cola.

        Parámetros
        ----------
        funcion_del_hilo : método
            Método que corre en el hilo (hilo_de_busqueda, hilo_de_lectura, ...).
        argumentos : tupla
            Valores que recibe ese método.
        mensaje : str
            Texto para la barra de estado mientras dura la tarea.
        """
        self.hay_tarea_en_curso = True
        self.habilitar_botones(False)
        self.set_status(mensaje, "warn")

        # NOTA (herramienta avanzada): usamos un hilo (threading.Thread).
        # Un hilo es una segunda línea de ejecución que corre en paralelo al
        # programa principal. Hace falta porque hablar con el osciloscopio por
        # GPIB tarda varios segundos y, sin el hilo, la ventana se congelaría.
        # target es el método que corre en el hilo y args sus valores.
        # daemon=True hace que el hilo se cierre solo si se cierra la ventana.
        hilo = threading.Thread(target=funcion_del_hilo, args=argumentos, daemon=True)
        hilo.start()
        self.id_revision_pendiente = self.after(INTERVALO_REVISION_MS, self.revisar_cola)

    def habilitar_botones(self, habilitar):
        """
        Habilita o deshabilita los botones que hablan con el osciloscopio.

        Parámetros
        ----------
        habilitar : bool
            True para habilitar, False para deshabilitar.
        """
        estado = ["disabled"]
        if habilitar:
            estado = ["!disabled"]
        for boton in [self.boton_buscar, self.boton_leer, self.boton_registro]:
            boton.state(estado)

    def publicar_error(self, error):
        """
        Pone un error en la cola con un mensaje legible. Se usa desde los hilos.

        Parámetros
        ----------
        error : Exception
            El error capturado.
        """
        if pyvisa is not None and isinstance(error, pyvisa.errors.VisaIOError):
            texto = ("El osciloscopio no respondió a tiempo o la comunicación falló. Revisa el "
                     f"cable GPIB, la dirección y que el osciloscopio esté en Talk/Listen.\n\n{error}")
        else:
            texto = str(error)
        self.cola_de_resultados.put(("error", texto))

    def hilo_de_busqueda(self):
        """Corre en un hilo: busca instrumentos y deja la lista en la cola."""
        try:
            recursos = buscar_instrumentos()
            self.cola_de_resultados.put(("recursos", recursos))
        except Exception as error:
            # Cualquier error se informa; si no, la ventana esperaría para siempre
            self.publicar_error(error)

    def hilo_de_lectura(self, nombre_recurso, canales_pedidos):
        """
        Corre en un hilo: lee el osciloscopio y deja la lectura en la cola.

        Parámetros
        ----------
        nombre_recurso : str
        canales_pedidos : lista de str
        """
        try:
            lectura = leer_osciloscopio(nombre_recurso, canales_pedidos, self.cola_de_resultados)
            self.cola_de_resultados.put(("lectura", lectura))
        except Exception as error:
            # Cualquier error se informa; si no, la ventana esperaría para siempre
            self.publicar_error(error)

    def hilo_de_registro_maximo(self, nombre_recurso):
        """
        Corre en un hilo: configura el registro máximo y deja la longitud en la cola.

        Parámetros
        ----------
        nombre_recurso : str
        """
        try:
            longitud = configurar_registro_maximo(nombre_recurso)
            self.cola_de_resultados.put(("registro", longitud))
        except Exception as error:
            # Cualquier error se informa; si no, la ventana esperaría para siempre
            self.publicar_error(error)

    def revisar_cola(self):
        """Atiende los mensajes de los hilos; si la tarea sigue, vuelve a revisar en un rato."""
        # Paso 1: atender todo lo que haya llegado
        while True:
            try:
                mensaje = self.cola_de_resultados.get_nowait()
            except queue.Empty:
                break
            self.atender_mensaje(mensaje)

        # Paso 2: seguir revisando mientras la tarea no termine
        if self.hay_tarea_en_curso:
            self.id_revision_pendiente = self.after(INTERVALO_REVISION_MS, self.revisar_cola)
        else:
            self.id_revision_pendiente = None

    def atender_mensaje(self, mensaje):
        """
        Hace lo que corresponde con un mensaje de un hilo.

        Parámetros
        ----------
        mensaje : tupla (tipo, contenido)
            tipo es "progreso", "error", "recursos", "lectura" o "registro".
        """
        tipo = mensaje[0]
        contenido = mensaje[1]

        # El avance no termina la tarea
        if tipo == "progreso":
            self.set_status(contenido, "warn")
            return

        # Todo lo demás marca el fin de la tarea
        self.hay_tarea_en_curso = False
        self.habilitar_botones(True)
        if tipo == "error":
            self.set_status("Error: " + contenido.split("\n")[0], "error")
            messagebox.showerror("Problema con el osciloscopio", contenido)
        elif tipo == "recursos":
            self.recibir_recursos(contenido)
        elif tipo == "lectura":
            self.recibir_lectura(contenido)
        elif tipo == "registro":
            self.set_status(f"Registro configurado en {contenido} puntos. Haz un nuevo disparo "
                            "antes de leer.", "ok")

    # ── Acciones de los botones ────────────────────────────────────────────

    def al_buscar(self):
        """Botón «Buscar»: lista los instrumentos conectados."""
        if not self.puede_empezar_tarea():
            return
        self.empezar_tarea(self.hilo_de_busqueda, (), "Buscando instrumentos…")

    def recibir_recursos(self, recursos):
        """
        Muestra los instrumentos encontrados en la lista desplegable.

        Parámetros
        ----------
        recursos : lista de str
        """
        self.lista_de_recursos["values"] = recursos
        if len(recursos) == 0:
            self.set_status("No se encontró ningún instrumento. Revisa el adaptador y su driver.", "error")
            return
        # Elegimos el primero (las direcciones GPIB vienen primero)
        self.texto_recurso.set(recursos[0])
        self.set_status(f"Encontrados: {', '.join(recursos)}", "ok")

    def canales_marcados(self):
        """
        Retorna
        -------
        lista de str
            Canales marcados en el panel, en orden.
        """
        marcados = []
        for canal in CANALES:
            if self.canal_marcado[canal].get():
                marcados.append(canal)
        return marcados

    def al_leer(self):
        """Botón «Leer osciloscopio»: lee base de tiempo y canales en un hilo."""
        if not self.puede_empezar_tarea():
            return
        canales = self.canales_marcados()
        if len(canales) == 0:
            messagebox.showinfo("Sin canales", "Marca al menos un canal para leer.")
            return
        recurso = self.texto_recurso.get().strip()
        self.empezar_tarea(self.hilo_de_lectura, (recurso, canales), f"Conectando con {recurso}…")

    def al_configurar_registro_maximo(self):
        """Botón «Registro de 15000 pts»: cambia la longitud de registro tras confirmar."""
        if not self.puede_empezar_tarea():
            return
        confirmado = messagebox.askyesno(
            "Cambiar el osciloscopio",
            f"Esto cambia la longitud de registro del osciloscopio a {LONGITUD_MAXIMA_REGISTRO} "
            "puntos. La señal que está en pantalla se mantiene hasta el próximo disparo.\n\n¿Continuar?")
        if not confirmado:
            return
        recurso = self.texto_recurso.get().strip()
        self.empezar_tarea(self.hilo_de_registro_maximo, (recurso,), "Configurando la longitud de registro…")

    def recibir_lectura(self, lectura):
        """
        Guarda una lectura nueva, la verifica y la muestra.

        Parámetros
        ----------
        lectura : LecturaDelOsciloscopio
        """
        self.lectura = lectura
        self.etiqueta_identificacion.config(text=lectura.identificacion)

        # Paso 1: verificaciones de cada canal y de la base de tiempo
        self.verificaciones = []
        verificaciones_por_canal = {}
        for traza in lectura.canales:
            resultados_del_canal = verificar_canal(traza)
            verificaciones_por_canal[traza.nombre] = resultados_del_canal
            for resultado in resultados_del_canal:
                self.verificaciones.append(resultado)
        verificaciones_horizontales = verificar_horizontal(lectura)
        for resultado in verificaciones_horizontales:
            self.verificaciones.append(resultado)

        # Paso 2: mostrar escalas, verificaciones y gráfico
        self.mostrar_escalas()
        self.mostrar_verificaciones(verificaciones_por_canal, verificaciones_horizontales)
        self.vista.set("pantalla")
        self.dibujar_grafico()

        # Paso 3: estado final
        total_de_puntos = 0
        for traza in lectura.canales:
            total_de_puntos = total_de_puntos + traza.numero_de_puntos()
        hay_problemas = len(lectura.avisos) > 0
        for resultado in self.verificaciones:
            if not resultado[0]:
                hay_problemas = True
        if hay_problemas:
            self.set_status(f"Lectura lista ({total_de_puntos} puntos), pero revisa los avisos del panel.", "warn")
        else:
            self.set_status(f"Lectura lista: {len(lectura.canales)} canales, {total_de_puntos} puntos. "
                            "Todo verificado.", "ok")

    def mostrar_escalas(self):
        """Escribe en el panel las escalas leídas del osciloscopio."""
        lectura = self.lectura
        formato_V_por_div = EngFormatter(unit="V/div", places=0)
        formato_s_por_div = EngFormatter(unit="s/div", places=0)
        formato_s = EngFormatter(unit="s", places=1)
        formato_V = EngFormatter(unit="V", places=1)
        lineas = []

        # Paso 1: una línea por canal, con la sonda si el osciloscopio la informó
        for traza in lectura.canales:
            linea = (f"{traza.nombre}  {formato_V_por_div(traza.volts_por_division_V):>11}"
                     f"  pos {traza.posicion_div:+.2f} div  offset {formato_V(traza.offset_V)}")
            if traza.atenuacion_sonda is not None:
                linea = linea + f"  sonda {traza.atenuacion_sonda:g}X"
            lineas.append(linea)
            # Descripción que da el propio osciloscopio (sirve para comparar con la pantalla)
            if "WFID" in traza.preambulo:
                lineas.append("   " + traza.preambulo["WFID"])

        # Paso 2: base de tiempo, registro y tramo visible
        primera = lectura.canales[0]
        intervalo_de_muestreo_s = primera.valor_del_preambulo("XINCR", 0.0)
        lineas.append(f"Base de tiempo  {formato_s_por_div(lectura.horizontal.segundos_por_division_s)}")
        lineas.append(f"Registro  {primera.numero_de_puntos()} pts, uno cada {formato_s(intervalo_de_muestreo_s)}")
        inicio_s, fin_s, es_exacta = calcular_ventana_de_pantalla(lectura)
        puntos_en_pantalla = 0
        for tiempo_s in primera.tiempos_s:
            if inicio_s <= tiempo_s <= fin_s:
                puntos_en_pantalla = puntos_en_pantalla + 1
        texto_ventana = "exacta"
        if not es_exacta:
            texto_ventana = "según posición horizontal"
        lineas.append(f"En pantalla  {puntos_en_pantalla} pts ({texto_ventana})")
        self.etiqueta_escalas.config(text="\n".join(lineas))

    def mostrar_verificaciones(self, verificaciones_por_canal, verificaciones_horizontales):
        """
        Escribe en el panel un resumen de las verificaciones y los avisos.

        Para que quepa, cada canal que pasó todo ocupa una sola línea con ✓.
        Lo que falló se muestra completo con ⚠. El detalle de todas las
        verificaciones queda en el encabezado del .csv.

        Parámetros
        ----------
        verificaciones_por_canal : dict de str a lista de tuplas (bool, str)
            Resultados de verificar_canal para cada canal.
        verificaciones_horizontales : lista de tuplas (bool, str)
            Resultados de verificar_horizontal.
        """
        lineas_correctas = []
        lineas_de_aviso = []

        # Paso 1: una línea por canal si todo está bien, o sus fallas
        for traza in self.lectura.canales:
            fallas = []
            for resultado in verificaciones_por_canal[traza.nombre]:
                if not resultado[0]:
                    fallas.append(resultado[1])
            if len(fallas) == 0:
                lineas_correctas.append(f"✓ {traza.nombre}: escala, centro y {traza.numero_de_puntos()} pts ok")
            else:
                for falla in fallas:
                    lineas_de_aviso.append("⚠ " + falla)

        # Paso 2: base de tiempo y avisos de la lectura
        for resultado in verificaciones_horizontales:
            if resultado[0]:
                lineas_correctas.append("✓ " + resultado[1])
            else:
                lineas_de_aviso.append("⚠ " + resultado[1])
        for aviso in self.lectura.avisos:
            lineas_de_aviso.append("⚠ " + aviso)

        # Paso 3: cada grupo en su etiqueta (los avisos van en color de advertencia)
        self.etiqueta_verificaciones.config(text="\n".join(lineas_correctas))
        self.etiqueta_avisos.config(text="\n".join(lineas_de_aviso))

    def al_guardar_csv(self):
        """Botón «Guardar CSV…»: guarda los datos y una imagen de la vista de pantalla."""
        # Paso 1: revisar que haya algo que guardar y que el tiempo sea común
        if self.lectura is None:
            messagebox.showinfo("Sin datos", "Primero lee el osciloscopio.")
            return
        for resultado in verificar_horizontal(self.lectura):
            if not resultado[0] and "otra base de tiempo" in resultado[1]:
                messagebox.showerror("Bases de tiempo distintas",
                                     resultado[1] + ". Desmarca ese canal y vuelve a leer.")
                return

        # Paso 2: preguntar los datos del disparo
        datos_del_disparo = self.pedir_datos_del_disparo()
        if datos_del_disparo is None:
            self.set_status("Guardado cancelado.", "idle")
            return

        # Paso 3: preguntar dónde guardar, con el número de disparo en el nombre
        nombre_sugerido = "tds684a_"
        disparo_para_nombre = limpiar_para_nombre_de_archivo(datos_del_disparo.numero_de_disparo)
        if disparo_para_nombre != "":
            nombre_sugerido = nombre_sugerido + "disparo" + disparo_para_nombre + "_"
        nombre_sugerido = nombre_sugerido + time.strftime("%Y%m%d_%H%M%S") + ".csv"
        ruta_csv = filedialog.asksaveasfilename(title="Guardar lectura", defaultextension=".csv",
                                                initialfile=nombre_sugerido,
                                                filetypes=[("Archivo CSV", "*.csv")])
        if ruta_csv == "":
            return
        ruta_base, extension = os.path.splitext(ruta_csv)
        ruta_png = ruta_base + ".png"

        # Paso 4: escribir el .csv y la imagen
        try:
            ventana = calcular_ventana_de_pantalla(self.lectura)
            encabezado = armar_encabezado(self.lectura, self.verificaciones, ventana, datos_del_disparo)
            escribir_csv(ruta_csv, self.lectura, encabezado)
            self.guardar_imagen_de_pantalla(ruta_png, datos_del_disparo)
        except OSError as error:
            messagebox.showerror("No se pudo guardar", f"{error}\n\nSi el archivo está abierto en "
                                 "otro programa, ciérralo e intenta de nuevo.")
            return
        self.ultimos_datos_del_disparo = datos_del_disparo
        self.set_status(f"Guardado: {os.path.basename(ruta_csv)} y {os.path.basename(ruta_png)}", "ok")

    def pedir_datos_del_disparo(self):
        """
        Abre el diálogo de datos del disparo y espera la respuesta.

        Los campos parten con los datos del disparo anterior y el número de
        disparo avanzado en uno, porque entre disparos suele cambiar solo eso.

        Retorna
        -------
        DatosDelDisparo o None
            None si el usuario canceló.
        """
        # Paso 1: valores con que parte el diálogo
        if self.ultimos_datos_del_disparo is None:
            datos_iniciales = DatosDelDisparo("", None, None, "")
        else:
            anteriores = self.ultimos_datos_del_disparo
            datos_iniciales = DatosDelDisparo(sugerir_siguiente_disparo(anteriores.numero_de_disparo),
                                              anteriores.diferencia_de_potencial_kV,
                                              anteriores.distancia_detector_cm, anteriores.gas)

        # Paso 2: mostrar el diálogo y esperar
        self.dialogo_abierto = DialogoDatosDelDisparo(self.master, datos_iniciales)
        datos = self.dialogo_abierto.mostrar()
        self.dialogo_abierto = None
        return datos

    def guardar_imagen_de_pantalla(self, ruta_png, datos_del_disparo):
        """
        Guarda la vista de pantalla como .png, aunque en la ventana se vea otra
        vista. Los datos del disparo van como título de la imagen.

        Parámetros
        ----------
        ruta_png : str
        datos_del_disparo : DatosDelDisparo
        """
        vista_actual = self.vista.get()
        self.vista.set("pantalla")
        self.dibujar_grafico()
        self.ejes.set_title(datos_del_disparo.resumen_corto(), fontsize=10, color=COLORS["text"], pad=14)
        # Reacomodar los márgenes para que el título quepa en la imagen
        self.figura.tight_layout()
        # Resolución de la imagen guardada, en puntos por pulgada
        puntos_por_pulgada_imagen = 150
        self.figura.savefig(ruta_png, dpi=puntos_por_pulgada_imagen, facecolor=COLORS["surface"])
        # Volver a dibujar la vista que estaba (sin el título)
        self.vista.set(vista_actual)
        self.dibujar_grafico()

    # ── Dibujo ─────────────────────────────────────────────────────────────

    def dibujar_grafico(self):
        """Dibuja la vista elegida: pantalla del osciloscopio o registro completo."""
        ejes = self.ejes
        ejes.clear()
        estilizar_ejes(ejes)

        # Sin lectura, solo un mensaje
        if self.lectura is None:
            ejes.text(0.5, 0.5, "Aquí aparecerá lo que muestra el osciloscopio", transform=ejes.transAxes,
                      ha="center", va="center", color=COLORS["muted"])
            ejes.set_xticks([])
            ejes.set_yticks([])
        elif self.vista.get() == "pantalla":
            self.dibujar_vista_pantalla(ejes)
        else:
            self.dibujar_vista_registro(ejes)

        self.figura.tight_layout()
        self.canvas.draw_idle()

    def dibujar_vista_pantalla(self, ejes):
        """
        Dibuja lo mismo que la pantalla: retícula de 10 × 8 divisiones, las
        trazas a su altura en divisiones, las flechas de 0 V y la marca del trigger.

        Parámetros
        ----------
        ejes : matplotlib.axes.Axes
        """
        lectura = self.lectura
        inicio_s, fin_s, es_exacta = calcular_ventana_de_pantalla(lectura)
        segundos_por_division_s = lectura.horizontal.segundos_por_division_s

        # Paso 1: unidad del eje de tiempo según la base de tiempo (por ejemplo
        # 100 ns/div → ns). Los tiempos se dividen por ese factor para que las
        # marcas muestren solo números y la unidad vaya una vez en el título.
        factor_tiempo, prefijo_tiempo = elegir_prefijo(segundos_por_division_s)
        inicio = inicio_s / factor_tiempo
        fin = fin_s / factor_tiempo

        # Paso 2: retícula con una línea en cada división, como en el osciloscopio
        marcas_horizontales = []
        for k in range(DIVISIONES_HORIZONTALES + 1):
            marca_s = inicio_s + k * segundos_por_division_s
            # Los decimales del computador dejan restos como 1e-22 s donde
            # debería haber 0; los borramos para que la marca diga "0"
            if abs(marca_s) < segundos_por_division_s * FRACCION_DE_DIVISION_DESPRECIABLE:
                marca_s = 0.0
            marcas_horizontales.append(marca_s / factor_tiempo)
        marcas_verticales_div = []
        for k in range(-MITAD_DIVISIONES_VERTICALES, MITAD_DIVISIONES_VERTICALES + 1):
            marcas_verticales_div.append(k)
        ejes.set_xticks(marcas_horizontales)
        ejes.set_yticks(marcas_verticales_div)
        ejes.grid(True, color="#C9CED6", linewidth=0.8)
        ejes.set_xlim(inicio, fin)
        ejes.set_ylim(-MITAD_DIVISIONES_VERTICALES, MITAD_DIVISIONES_VERTICALES)

        # Paso 3: cada canal a su altura en la pantalla, con su flecha de 0 V a la izquierda
        formato_V_por_div = EngFormatter(unit="V/div", places=0)
        for traza in lectura.canales:
            color = COLORES_DE_CANALES[traza.nombre]
            etiqueta = f"{traza.nombre}  {formato_V_por_div(traza.volts_por_division_V)}"
            # División sobre todo el arreglo a la vez: tiempos en la unidad del eje
            tiempos = traza.tiempos_s / factor_tiempo
            ejes.plot(tiempos, traza.divisiones_en_pantalla, color=color, linewidth=1.0, label=etiqueta)
            nivel_de_cero_div = traza.nivel_de_cero_en_divisiones()
            if abs(nivel_de_cero_div) <= MITAD_DIVISIONES_VERTICALES:
                ejes.plot(inicio, nivel_de_cero_div, marker=">", markersize=8, color=color, clip_on=False)

        # Paso 4: marca del trigger arriba, si cae dentro de la pantalla
        if inicio_s <= 0.0 <= fin_s:
            ejes.plot(0.0, MITAD_DIVISIONES_VERTICALES, marker="v", markersize=8,
                      color=COLORS["text"], clip_on=False)

        # Paso 5: títulos de los ejes con la unidad, marcas solo con números
        formato_s_por_div = EngFormatter(unit="s/div", places=0)
        titulo_x = (f"tiempo desde el trigger ({prefijo_tiempo}s)  ·  "
                    f"{formato_s_por_div(segundos_por_division_s)}")
        if not es_exacta:
            titulo_x = titulo_x + "  ·  tramo según posición horizontal"
        ejes.set_xlabel(titulo_x)
        ejes.set_ylabel("divisiones")
        # Sin desplazamiento automático: cada marca muestra su valor completo
        ejes.ticklabel_format(axis="x", useOffset=False, style="plain")
        ejes.legend(fontsize=8, frameon=True, loc="upper right", framealpha=0.9)

    def dibujar_vista_registro(self, ejes):
        """
        Dibuja todos los puntos de todos los canales en volts, con el tramo
        que se ve en la pantalla sombreado.

        Parámetros
        ----------
        ejes : matplotlib.axes.Axes
        """
        lectura = self.lectura
        inicio_s, fin_s, es_exacta = calcular_ventana_de_pantalla(lectura)

        # Paso 1: unidades de los ejes según los valores más grandes a mostrar
        primera = lectura.canales[0]
        tiempo_mas_lejano_s = max(abs(float(primera.tiempos_s[0])), abs(float(primera.tiempos_s[-1])))
        voltaje_mas_grande_V = 0.0
        for traza in lectura.canales:
            voltaje_mas_grande_V = max(voltaje_mas_grande_V, float(np.max(np.abs(traza.voltajes_V))))
        factor_tiempo, prefijo_tiempo = elegir_prefijo(tiempo_mas_lejano_s)
        factor_voltaje, prefijo_voltaje = elegir_prefijo(voltaje_mas_grande_V)

        # Paso 2: tramo en pantalla y cada canal, en esas unidades
        ejes.axvspan(inicio_s / factor_tiempo, fin_s / factor_tiempo, color=COLORS["border"],
                     alpha=0.5, linewidth=0, label="tramo en pantalla")
        for traza in lectura.canales:
            # Divisiones sobre todo el arreglo a la vez
            tiempos = traza.tiempos_s / factor_tiempo
            voltajes = traza.voltajes_V / factor_voltaje
            ejes.plot(tiempos, voltajes, color=COLORES_DE_CANALES[traza.nombre],
                      linewidth=0.9, label=f"{traza.nombre}  ({traza.numero_de_puntos()} pts)")

        # Paso 3: títulos con la unidad, marcas solo con números
        ejes.set_xlabel(f"tiempo desde el trigger ({prefijo_tiempo}s)")
        ejes.set_ylabel(f"voltaje ({prefijo_voltaje}V)")
        ejes.ticklabel_format(axis="both", useOffset=False, style="plain")
        ejes.legend(fontsize=8, frameon=False, loc="best")

    # ── Cierre ─────────────────────────────────────────────────────────────

    def al_cerrar(self):
        """Cierra la ventana cancelando la revisión pendiente de la cola."""
        if self.id_revision_pendiente is not None:
            self.after_cancel(self.id_revision_pendiente)
            self.id_revision_pendiente = None
        # Los hilos son daemon: se cierran solos. Cada función de lectura
        # cierra su conexión en un finally, así que el adaptador queda libre.
        self.master.destroy()


# ─── Nitidez y posición de la ventana (de la plantilla de interfaces) ──────

def set_dpi_awareness():
    """
    En Windows, avisa al sistema que la ventana maneja su propia escala para
    que no se vea borrosa en pantallas de alta resolución. Debe llamarse antes
    de crear tk.Tk(). Fuera de Windows no hace nada.
    """
    if sys.platform != "win32":
        return
    # ctypes permite llamar funciones de Windows desde Python
    import ctypes
    # Intento 1: la mejor calidad, por monitor (Windows 10 versión 1703 o más nuevo)
    try:
        funcion_contexto = ctypes.windll.user32.SetProcessDpiAwarenessContext
        funcion_contexto.argtypes = [ctypes.c_void_p]
        funcion_contexto.restype = ctypes.c_bool
        # -4 es el código de Windows para "PER_MONITOR_AWARE_V2"
        if funcion_contexto(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    # Intento 2: por monitor (Windows 8.1 o más nuevo)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    # Intento 3: según el DPI del sistema (Windows Vista o más nuevo)
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def tune_scaling(root, minimo=1.0):
    """
    Ajusta la escala de tkinter a la resolución real de la pantalla.

    Parámetros
    ----------
    root : tk.Tk
        Ventana principal.
    minimo : float
        Factor mínimo a devolver.

    Retorna
    -------
    float
        Factor respecto a 96 puntos por pulgada (el 100 % estándar).
    """
    puntos_por_pulgada = root.winfo_fpixels("1i")
    # tkinter mide las fuentes en puntos; 72 puntos = 1 pulgada
    root.tk.call("tk", "scaling", puntos_por_pulgada / 72.0)
    return max(puntos_por_pulgada / 96.0, minimo)


def center(win, w, h):
    """
    Centra la ventana en la pantalla, sin que sea más grande que la pantalla.

    Parámetros
    ----------
    win : tk.Tk
        Ventana a centrar.
    w, h : int
        Ancho y alto deseados, en píxeles.
    """
    win.update_idletasks()
    ancho_pantalla = win.winfo_screenwidth()
    alto_pantalla = win.winfo_screenheight()
    # Dejamos un margen para la barra de tareas y los bordes de la ventana
    margen_px = 80
    ancho = min(w, ancho_pantalla - margen_px)
    alto = min(h, alto_pantalla - margen_px)
    x = (ancho_pantalla - ancho) // 2
    y = (alto_pantalla - alto) // 3
    win.geometry(f"{ancho}x{alto}+{x}+{y}")


def main():
    """Crea la ventana y arranca el programa."""
    set_dpi_awareness()
    raiz = tk.Tk()
    raiz.title("Osciloscopio TDS 684A · GPIB")
    escala = tune_scaling(raiz)
    raiz.minsize(int(980 * escala), int(640 * escala))
    setup_style(raiz)
    center(raiz, int(1240 * escala), int(780 * escala))
    aplicacion = App(raiz, escala)
    # Al cerrar la ventana con la X, pasar por al_cerrar para terminar limpio
    raiz.protocol("WM_DELETE_WINDOW", aplicacion.al_cerrar)
    raiz.mainloop()


# Programa principal. Solo se ejecuta si corremos este archivo directamente,
# no cuando otro archivo lo importa para usar sus funciones.
if __name__ == "__main__":
    main()
