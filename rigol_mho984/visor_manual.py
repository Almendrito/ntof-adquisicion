import tkinter as tk
from tkinter import filedialog
import h5py
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

class VisorPulsosDAQ:
    def __init__(self, root):
        self.root = root
        self.root.title("Visor de Pulsos - Física Experimental")
        self.root.geometry("800x600")
        
        self.archivo_h5 = None
        self.nombres_pulsos = []
        self.indice_actual = 0
        self.grupo_actual = None
        
        # --- 1. Panel de Controles ---
        frame_top = tk.Frame(root)
        frame_top.pack(side=tk.TOP, fill=tk.X, padx=10, pady=10)
        
        self.btn_abrir = tk.Button(frame_top, text="Abrir Archivo .h5", command=self.abrir_archivo)
        self.btn_abrir.pack(side=tk.LEFT)
        
        self.btn_prev = tk.Button(frame_top, text="<< Anterior", command=self.prev_pulso, state=tk.DISABLED)
        self.btn_prev.pack(side=tk.LEFT, padx=10)
        
        self.btn_next = tk.Button(frame_top, text="Siguiente >>", command=self.next_pulso, state=tk.DISABLED)
        self.btn_next.pack(side=tk.LEFT)
        
        self.lbl_estado = tk.Label(frame_top, text="Ningún archivo cargado")
        self.lbl_estado.pack(side=tk.RIGHT)
        
        # --- 2. Lienzo del Gráfico (Matplotlib) ---
        self.fig, self.ax = plt.subplots()
        self.canvas = FigureCanvasTkAgg(self.fig, master=root)
        self.canvas.get_tk_widget().pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True)
        
    def abrir_archivo(self):
        ruta = filedialog.askopenfilename(filetypes=[("Archivos HDF5", "*.h5")])
        if not ruta:
            return
            
        if self.archivo_h5:
            self.archivo_h5.close()
            
        try:
            self.archivo_h5 = h5py.File(ruta, 'r')
            
            # Detectar el grupo de datos según la etapa (crudos o filtrados)
            if "pulsos_crudos" in self.archivo_h5:
                self.grupo_actual = self.archivo_h5["pulsos_crudos"]
            elif "pulsos" in self.archivo_h5:
                self.grupo_actual = self.archivo_h5["pulsos"]
            else:
                self.lbl_estado.config(text="Error: Formato interno desconocido.")
                return
                
            self.nombres_pulsos = list(self.grupo_actual.keys())
            
            # Ordenar numéricamente los nombres (pulso_0, pulso_1, pulso_2...)
            self.nombres_pulsos.sort(key=lambda x: int(x.split('_')[1]))
            
            self.indice_actual = 0
            self.btn_prev.config(state=tk.NORMAL)
            self.btn_next.config(state=tk.NORMAL)
            self.actualizar_grafico()
            
        except Exception as e:
            self.lbl_estado.config(text=f"Error al abrir: {e}")

    def actualizar_grafico(self):
        if not self.nombres_pulsos:
            return
            
        nombre = self.nombres_pulsos[self.indice_actual]
        datos_crudos = self.grupo_actual[nombre][:]
        
        # Limpiar gráfico anterior y dibujar el nuevo
        self.ax.clear()
        
        # 'steps-mid' da la apariencia real de escalera digital típica de DAQs
        self.ax.plot(datos_crudos, color='blue', drawstyle='steps-mid', linewidth=1)
        
        self.ax.set_title(f"Visualizando: {nombre}")
        self.ax.set_ylabel("Amplitud Binaria (0 - 255)")
        self.ax.set_xlabel("Muestras de Tiempo")
        self.ax.grid(True, linestyle='--', alpha=0.6)
        
        self.canvas.draw()
        self.lbl_estado.config(text=f"Pulso {self.indice_actual + 1} de {len(self.nombres_pulsos)}")

    def prev_pulso(self):
        if self.indice_actual > 0:
            self.indice_actual -= 1
            self.actualizar_grafico()

    def next_pulso(self):
        if self.indice_actual < len(self.nombres_pulsos) - 1:
            self.indice_actual += 1
            self.actualizar_grafico()

if __name__ == "__main__":
    ventana_principal = tk.Tk()
    app = VisorPulsosDAQ(ventana_principal)
    ventana_principal.mainloop()