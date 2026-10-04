"""Meyton/DSB rule catalog and custom discipline-name configuration."""
import json, os, re, tempfile
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GEOMETRIES = {
 "air_rifle":{"shape":"rings","diameters":[45.5,40.5,35.5,30.5,25.5,20.5,15.5,10.5,5.5,.5],"black_diameter":30.5},
 "air_pistol":{"shape":"rings","diameters":[155.5,139.5,123.5,107.5,91.5,75.5,59.5,43.5,27.5,11.5],"black_diameter":59.5},
 "rifle_50m":{"shape":"rings","diameters":[154.4,138.4,122.4,106.4,90.4,74.4,58.4,42.4,26.4,10.4],"black_diameter":112.4},
 "pistol_precision":{"shape":"rings","diameters":[500,450,400,350,300,250,200,150,100,50],"black_diameter":200},
 "pistol_rapid":{"shape":"rings","diameters":[500,400,300,200,100],"values":[6,7,8,9,10],"black_diameter":500},
 "schach10":{"shape":"schach10","size":90},
}
def rule(caliber, description, geometry=None, positions=None, group="DSB"):
 return {"caliber_mm":caliber,"description":description,"geometry":geometry,"positions":positions or {},"group":group}
RULES = {
 "0100":rule(4.5,"Luftgewehr","air_rifle"),"0110":rule(4.5,"Luftgewehr","air_rifle"),"0111":rule(4.5,"Luftgewehr Auflage","air_rifle"),"0120":rule(4.5,"Luftgewehr Dreistellung","air_rifle"),
 "0130":rule(4.65,"Zimmerstutzen"),"0135":rule(5.6,"KK 100 m"),"0140":rule(5.6,"KK Sportgewehr","rifle_50m"),"0141":rule(5.6,"KK Auflage","rifle_50m"),"0142":rule(5.6,"KK 50 m Zielfernrohr","rifle_50m"),"0160":rule(5.6,"KK Freigewehr","rifle_50m"),"0180":rule(5.6,"KK liegend","rifle_50m"),
 "0193":rule(4.5,"Luftgewehr Zehntelring","air_rifle"),"0194":rule(4.5,"Luftgewehr Zehntelring","air_rifle"),"0195":rule(4.5,"Luftgewehr Zehntelring","air_rifle"),
 "0210":rule(4.5,"Luftpistole","air_pistol"),"0211":rule(4.5,"Luftpistole Auflage","air_pistol"),"0216":rule(4.5,"Mehrschüssige Luftpistole","air_pistol"),"0217":rule(4.5,"Luftpistole Mehrkampf","air_pistol"),"0218":rule(4.5,"Luftpistole Standard","air_pistol"),
 "0220":rule(5.6,"50 m Pistole","pistol_precision"),"0230":rule(5.6,"25 m Schnellfeuerpistole","pistol_rapid"),"0231":rule(5.6,"25 m Schnellfeuerpistole Nachwuchs","pistol_rapid"),
 "0240":rule(5.6,"25 m Pistole",positions={"1":"pistol_precision","2":"pistol_rapid"}),"0241":rule(5.6,"25 m Pistole Nachwuchs",positions={"1":"pistol_precision","2":"pistol_rapid"}),"0245":rule([7.62,9.65],"25 m Zentralfeuerpistole",positions={"1":"pistol_precision","2":"pistol_rapid"}),
 "0253":rule(9.0,"25 m Pistole 9 mm","pistol_precision"),"0255":rule(9.07,"25 m Revolver .357","pistol_precision"),"0258":rule(11.18,"25 m Revolver .44","pistol_precision"),"0259":rule(11.43,"25 m Pistole .45","pistol_precision"),"0260":rule(5.6,"25 m Standardpistole","pistol_precision"),
 "0610":rule(4.5,"ISSF Air Rifle Final","air_rifle",group="ISSF"),"0611":rule(4.5,"ISSF Air Pistol Final","air_pistol",group="ISSF"),"0710":rule(4.5,"ISSF Air Rifle","air_rifle",group="ISSF"),"0740":rule(5.6,"ISSF 50 m Rifle","rifle_50m",group="ISSF"),"0810":rule(4.5,"ISSF Air Pistol","air_pistol",group="ISSF"),"0830":rule(5.6,"ISSF Rapid Fire Pistol","pistol_rapid",group="ISSF"),
 "0831":rule(5.6,"ISSF 25 m Pistol",positions={"1":"pistol_precision","2":"pistol_rapid"},group="ISSF"),"0832":rule([7.62,9.65],"ISSF Center Fire Pistol",positions={"1":"pistol_precision","2":"pistol_rapid"},group="ISSF"),"0833":rule(5.6,"ISSF Standard Pistol","pistol_precision",group="ISSF"),"0840":rule(5.6,"ISSF 50 m Pistol","pistol_precision",group="ISSF"),
 "1009":rule(4.5,"Luftgewehr Training","air_rifle",group="Meyton"),"1010":rule(4.5,"Luftgewehr Training","air_rifle",group="Meyton"),"1011":rule(4.5,"Luftgewehr Training","air_rifle",group="Meyton"),"1012":rule(4.5,"Luftgewehr Training","air_rifle",group="Meyton"),
 "1109":rule(4.5,"Luftpistole Training","air_pistol",group="Meyton"),"1110":rule(4.5,"Luftpistole Training","air_pistol",group="Meyton"),"1111":rule(4.5,"Luftpistole Training","air_pistol",group="Meyton"),"1112":rule(4.5,"Luftpistole Training","air_pistol",group="Meyton"),
 "1209":rule(5.6,"KK Training","rifle_50m",group="Meyton"),"1211":rule(5.6,"KK Training","rifle_50m",group="Meyton"),"1212":rule(5.6,"KK Training","rifle_50m",group="Meyton"),"8052":rule(4.5,"Schach 10","schach10",group="Meyton"),
}
CUSTOM_RULES={"Schach10":rule(4.5,"Schach 10","schach10",group="Custom")}; ALL_RULES=RULES|CUSTOM_RULES
class DisciplineMapping(BaseModel):
 model_config=ConfigDict(extra="forbid"); name:str=Field(min_length=1,max_length=64); rule:str
 @field_validator("name")
 @classmethod
 def trim_name(cls,v):
  v=v.strip()
  if not v: raise ValueError("Disziplinname darf nicht leer sein")
  return v
 @field_validator("rule")
 @classmethod
 def known_rule(cls,v):
  if v not in ALL_RULES: raise ValueError("Unbekannte Regelnummer")
  return v
class DisciplineMappings(BaseModel):
 model_config=ConfigDict(extra="forbid"); mappings:list[DisciplineMapping]=Field(max_length=64)
 @model_validator(mode="after")
 def unique_names(self):
  names=[m.name for m in self.mappings]
  if len(names)!=len(set(names)): raise ValueError("Disziplinnamen müssen eindeutig sein")
  return self
class DisciplineStore:
 def __init__(self,path):
  self.path=Path(path); self.configuration=DisciplineMappings.model_validate_json(self.path.read_text()) if self.path.exists() else DisciplineMappings(mappings=[])
  if not self.path.exists(): self.save(self.configuration)
 @property
 def rules(self): return {m.name:m.rule for m in self.configuration.mappings}
 def save(self,configuration):
  self.path.parent.mkdir(parents=True,exist_ok=True); descriptor,temporary=tempfile.mkstemp(dir=self.path.parent,prefix=".disciplines-",suffix=".json")
  try:
   with os.fdopen(descriptor,"w") as f: json.dump(configuration.model_dump(),f,ensure_ascii=False,indent=2); f.write("\n"); f.flush(); os.fsync(f.fileno())
   os.replace(temporary,self.path); self.configuration=configuration
  finally:
   if os.path.exists(temporary): os.unlink(temporary)
def resolve_target_rule(discipline_id,name,custom_rules=None):
 encoded=str(discipline_id)
 return encoded[1:5] if re.fullmatch(r"\d{8}",encoded) else (custom_rules or {}).get(name)
def public_catalog(): return {"revision":1,"rules":[{"rule":k,**v} for k,v in ALL_RULES.items()],"geometries":GEOMETRIES}
