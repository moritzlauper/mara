# Konzept: Pegelbasis, kalibriertes Modell und Nachweis

Stand 14.11.2026. Baut auf [konzept-solver.md](konzept-solver.md) auf.

## Befund

Als selbständiger Mixer reicht Mara für Profis noch nicht. Als Messassistent für Tonalität, Bass-Mono und Lautheit gegen Referenzen ist die Grundlage brauchbar: Jede Änderung ist begrenzt, wird gemessen und lässt sich zurücknehmen, und die Messung ist wiederholgenau. Es fehlen vier Dinge: Ein definierter Pegelbezug, ein Zielmodell über das Master-Spektrum hinaus, die Rückkopplung von der Messung ins Modell und ein Nachweis, dass die Zahlen mit dem Höreindruck zusammenhängen.

Geprüft habe ich das am Code und an den Daten in `songs/*/runs`:

| Prüfung | Ergebnis |
| --- | --- |
| Lautheitsschätzung aus den 31 Bändern gegen BS.1770 ohne Gate, an der Vorher-Aufnahme vom 10.11. und an «So U Know» | Abweichung +0.01 und -0.07 LU. Der Meter stimmt, daran ist nichts zu ändern. |
| Streuung zwischen zwei Messdurchgängen, 25 Runden aus den Journalen | Tonal: Median 0.03, 90. Perzentil 0.11. Masking: Median 0.14, 90. Perzentil 0.40, Maximum 0.84. |
| Runden des alten Ablaufs | 132 behalten mit einem Median-Gewinn von 0.10 bei einer Distanz um 10.5. 135 angewendete Runden wurden zurückgenommen. |
| Solver-Ablauf in einer echten Session | Kein Log vorhanden. Er lief bisher nur im Simulator, und der rechnet mit Spektren statt mit Audio. |
| Referenz «So U Know» | Sample-Peak 0.00 dBFS, lauteste Stelle -7.22 LUFS, PLR 7.2 dB. Mit Oversampling gemessen liegt der True Peak bei etwa +2.9 dBTP. |

Die Messung ist also genau genug. Die Schwächen liegen darin, was gemessen wird und was Mara daraus ableitet:

1. Es gibt keinen Pegelbezug. Die Grenzen für Limiter und Clipper gelten für den Regler, nicht für die Gain Reduction (siehe nächster Abschnitt).
2. Das einzige Ziel ist ein Spektrum. 34 Kanäle mal 31 Bänder stehen 31 Master-Bändern gegenüber, die Lösung ist also stark unterbestimmt. Welcher Kanal nachgibt, entscheiden die Prioritätsgewichte und keine Messung. Ob Kick und Bass zueinander passen oder die Drums gegenüber dem Rest zu leise sind, prüft nichts.
3. Mit einer einzigen Referenz beträgt die Toleranz 0.5 dB pro Terzband (`ReferenceSet.target`). Damit wird ein Song samt Tonart zur Norm, denn der Grundton des Basses fällt in ein bestimmtes Terzband.
4. Das Modell wird nie kalibriert. Der Log zeigt Vorhersage und Messung nebeneinander, die Abweichung fliesst aber nirgends ein. Die Annahme unkorrelierter Kanäle ist bei Kick und Sub, bei gelayerten Snares und bei Parallelwegen falsch, also dort, wo die Vorhersage am meisten zählt. Phase und Polarität sieht Mara gar nicht.
5. Masking ist eine paarweise Heuristik und die ungenaueste Messgrösse. Die Summe wächst mit der Kanalzahl: Im Lauf mit 34 Kanälen waren es 7.25 von 10.60 Punkten. Verdeckt wird ein Instrument aber vom ganzen Restmix und nicht von einem einzelnen Partner.
6. Dichte und Ceiling sind vermischt. PLR ist Peak minus Lautheit. Die Referenz reicht bis 0.0 dBFS, Maras Limiter bis -1.0 dBFS. Gleiche Lautheit bedeutet deshalb zwangsläufig 1 dB weniger PLR, und das ist genau die erlaubte Marge von 1 dB.
7. Das Master-Paket wird nur an der lautesten Stelle gerechnet und geprüft. In den anderen Passagen prüft der Solver-Ablauf nur Kollisionen, und zwar gegen eine Basis, die nach dem Master-Paket gemessen wurde. Was die neuen EQs im Intro oder im Break anrichten, vergleicht niemand mit dem Original.
8. Der Solver kennt Bell mit Q 1.0, Low Cut, Fader, Bass-Mono und Ducking. Shelves fehlen, die Breite wird nie verändert, Kompression kommt nur in höchstens zwei freien Modellrunden vor und mit «Built-in rules» gar nicht.
9. Ein Nachweis fehlt. Die UI bietet einen Blindvergleich, die Wahl wird aber nicht gespeichert.
10. Im Tap entspricht `average~ 4410` nur bei 44.1 kHz den 100 ms eines Frames. Bei 96 kHz sind es 46 ms, dann bleibt mehr als die Hälfte des Signals unbeobachtet. Die Frames tragen ausserdem die Ankunftszeit statt der Songposition.

## Gain Staging: Ja, als Pegelbasis

Mara sollte mit Gain Staging beginnen, allerdings nicht mit der Regel «jeder Kanal auf -18 dBFS». Live rechnet in Gleitkomma, lineare Geräte verhalten sich bei jedem Pegel gleich, und die Taps messen ohnehin vor dem Fader. Ein Trim vor den Kompressoren und Sättigern des Nutzers würde dagegen deren Klang verändern. Das wäre ein Eingriff ohne messbaren Nutzen.

Ein Pegelbezug wird an einer Stelle gebraucht: Am Eingang der Lautheitskette. `LIMITER_MAX_GAIN_DB = 5.0` ist im Code als «about 4–5 dB of peak gain reduction» kommentiert ([actions.py:126](../mixmatch/actions.py#L126)). Das stimmt nur, wenn die Summe vor dem Limiter etwa bei der Ceiling spitzt. Spitzt sie bei -8 dBFS, ergeben +5 dB Limiter und +4 dB Clipper keine Gain Reduction, und der Mix bleibt leise. Spitzt sie bei +4 dBFS, arbeitet der Limiter schon vor Maras erstem Schritt mit 5 dB. Zurücknehmen kann Mara das nicht: Der Limiter-Gain ist nach unten auf 0 begrenzt ([actions.py:471](../mixmatch/actions.py#L471)), und `set_loudness` unternimmt nichts, wenn der Mix lauter ist als die Referenz ([engine.py:512](../mixmatch/engine.py#L512)). Die Grenzen +5 und +4 dB sind an einem Projekt nach Gehör gesetzt worden. In einem anderen Projekt bedeuten sie etwas anderes.

Die Pegelbasis besteht aus vier Teilen:

1. Ein zweiter Tap auf Main misst vor Clipper und Limiter. Aus beiden Taps ergeben sich die Gain Reduction und die tonale Wirkung der Kette als Messwerte.
2. Ein Utility «MARA Trim» direkt vor dem Clipper stellt die Summe so ein, dass ihr Peak (99. Perzentil der Frame-Peaks an der lautesten Stelle) auf der Ceiling liegt. Es sitzt hinter den Geräten des Nutzers auf Main, sein Buskompressor reagiert also wie bisher. Kanäle, Gruppen und Balance bleiben unberührt, und der Schritt ist mit einem Parameter rückgängig zu machen.
3. Die Grenzen gelten danach für die gemessene Gain Reduction: Limiter höchstens 5 dB, Clipper höchstens 4 dB. Das entspricht der Absicht hinter den heutigen Werten und gilt in jedem Projekt gleich.
4. Nach jedem behaltenen Paket wird der Trim nachgeführt. Heute senkt ein Paket von Schnitten den Pegel am Limiter, die Lautheit fällt, die PLR steigt, und `evaluate` beurteilt die Tonalität mit verschobenen Lautheits- und Dynamikwerten. Mit gehaltener Pegelbasis ändert jedes Paket eine Grösse.

Dazu kommt ein Pegelbericht ohne Eingriff: Kanäle, Gruppen und Returns, die am Tap über 0 dBFS spitzen, sowie Fader am Anschlag. Das sind Hinweise an den Nutzer, weil die Ursache meist ein Gerät in seiner Kette ist.

## Neuer Ablauf

| Phase | Was passiert | Prüfung |
| --- | --- | --- |
| 0 Pegelbasis | Zweiter Tap, Trim, Pegelbericht | Peak vor der Kette auf der Ceiling, Gain Reduction gemessen |
| 1 Diagnose | Basis viermal messen, daraus die Streuung pro Messgrösse und Band. Summe der Kanäle gegen den Master vergleichen (Kohärenzrest), Gruppen auf Auslöschung prüfen | Bericht, keine Änderung |
| 2 Balance | Nur Fader. Ein Kanal, der über den grössten Teil seines Kernbereichs in dieselbe Richtung abweicht, hat ein Pegelproblem und bekommt keinen EQ | Paket messen, Trim nachführen |
| 3 Tonalität | Bells, neu auch Shelves, Low Cuts. Schrittweite aus dem Verhältnis von gemessener zu vorhergesagter Wirkung | Paket messen, Trim nachführen |
| 4 Verdeckung | Jeder Vordergrundkanal gegen den Restmix, Schnitt bei den grössten Verursachern | Pro Vordergrundkanal |
| 5 Lautheit | Limiter und Clipper nach gemessener Gain Reduction | Gegen den Originalzustand |
| 6 Nachweis | Ganzer Song noch einmal, Normwerte aus der Aufnahme, Blindwahl speichern | Kein Rückschritt in einer Passage |

Heute konkurrieren Fader und EQ in einer gemeinsamen Suche, und der Fader ist mit dem Gewicht 0.6 benachteiligt. Ein Fader hat aber keine Nebenwirkung auf Klang und Phase. Deshalb kommt er zuerst, und der EQ behandelt nur, was danach übrig ist.

## Messung

Der Tap bekommt eine zweite Version. Sie sendet zusätzlich die Songposition in Beats, damit Messfenster nach Position gewählt werden statt nach der Ankunftszeit des UDP-Pakets. Die RMS-Länge richtet sich nach der Samplerate. Das Seitensignal wird in vier Bändern gemessen, damit Breite pro Bereich vergleichbar wird. Bestehende Sets brauchen danach einmal «Set taps».

Normwerte für den Master kommen aus den Aufnahmen, die für Vorher und Nachher ohnehin entstehen: Integrierte Lautheit und Short-Term-Maximum nach BS.1770, Loudness Range und True Peak mit vierfachem Oversampling. Der True Peak fehlt heute ganz.

Die Streuung wird einmal pro Passage aus vier Messungen der Basis bestimmt. Heute ist sie die Spannweite aus zwei Messungen, und zwei Werte liegen oft zufällig nahe beieinander. Die Annahmeschwelle wird das Doppelte der Standardabweichung.

## Zielmodell

Die Toleranz bekommt einen Boden aus der Streuung vieler Songs. Bei weniger als drei Referenzen gilt pro Band mindestens die Streuung der eigenen Referenzbibliothek. Bis diese ausgemessen ist, schlage ich 1.5 dB in den Mitten und 3 dB unter 80 Hz und über 8 kHz vor. Diese Startwerte sind eine Annahme und nicht gemessen.

Der Bereich 30–120 Hz wird als Summe verglichen. So hängt das Ergebnis nicht davon ab, in welches Terzband der Grundton fällt.

Als zweite Zielgrösse kommt ein Dynamikprofil pro Band dazu: Der Abstand zwischen dem 95. und dem 50. Perzentil der Bandpegel über die Zeit. Die Daten dafür liegen in den 100-ms-Frames von Mix und Referenz bereits vor. Das Profil zeigt, ob in einem Band Anschläge oder Flächen dominieren, und gibt der Balance-Phase ein Mass für Fragen wie «Kick zu leise gegenüber dem Bass» oder «Hats zu laut». Ob es dafür taugt, muss der Fehlertest unten zeigen.

Die Dichte wird bei gleicher Ceiling verglichen, mit dem 99. Perzentil der Peaks statt dem Maximum.

Die Gesamtzahl «distance» bleibt als Anzeige, entscheidet aber nichts mehr. Sie addiert dB, LU und einheitenlose Werte mit frei gewählten Gewichten. Stattdessen steht pro Messgrösse eine Zeile im Log: Ist, Ziel, Toleranz, Streuung.

## Modell kalibrieren

Nach jedem Paket wird das Verhältnis von gemessener zu vorhergesagter Verbesserung berechnet. Über 0.75 bleibt das Paket und der nächste Schritt darf grösser werden. Zwischen 0.25 und 0.75 bleibt es bei gleicher Schrittweite. Unter 0.25 wird es zurückgenommen, die Schrittweite halbiert, und die Bänder mit der grössten Abweichung brauchen im nächsten Durchgang mehr vorhergesagten Nutzen. Das ist das übliche Trust-Region-Verfahren. Heute folgt ein zweiter Versuch mit halber Stärke nur, wenn eine Schutzregel angeschlagen hat ([engine.py:352](../mixmatch/engine.py#L352)).

Der Kohärenzrest aus der Diagnose ist die Differenz zwischen dem gemessenen Master vor der Kette und der Leistungssumme der Kanäle, pro Band. Wo er über 2 dB liegt, ist die Vorhersage unzuverlässig, entweder wegen korrelierter Quellen oder wegen Bus-Bearbeitung. Dort bevorzugt der Solver die Gruppe. Liegt eine Gruppe im Bass mehr als 2 dB unter der Summe ihrer Mitglieder, meldet Mara eine mögliche Auslöschung. Das ist der Polaritätstest, den ein Engineer vor allem anderen macht.

Layer gelten als Einheit. Ein EQ auf einem von zwei korrelierten Layern verschiebt dessen Phase gegen den anderen, und die Summe ändert sich anders als vorhergesagt.

## Verdeckung

Für jeden Vordergrundkanal wird in seinem Kernbereich und nur während er spielt der Abstand zwischen seinem Pegel und der Summe aller anderen Kanäle berechnet, mit Ausbreitung auf die Nachbarbänder. Der Schnitt geht an die Kanäle mit dem grössten Anteil am Restmix in diesem Band. Der Wert wird pro Vordergrundkanal ausgewiesen und nicht über alle Paare addiert, damit er nicht mit der Kanalzahl wächst.

## Nachweis

1. Der Solver-Ablauf läuft einmal in einer echten Session. Der Log liefert pro Paket das Verhältnis von Messung zu Vorhersage. Das ist die erste Zahl darüber, ob das Modell trägt.
2. Ein Fehlertest ausserhalb von Live: Stems eines Projekts werden in Python summiert, ein bekannter Fehler wird eingebaut (+4 dB bei 300 Hz auf einem Pad, Stereo-Sub, Hats +3 dB), und der Solver muss ihn finden. Gemessen werden die Trefferquote und die Zahl der Änderungen an Kanälen ohne Fehler. EQ und Fader sind linear und lassen sich exakt rechnen.
3. Jede Blindwahl zwischen Vorher und Nachher wird im Journal gespeichert. Nach genügend Läufen zeigt sich, ob Mara häufiger gewinnt als der Zufall und welche Arten von Änderungen dazu beitragen. Am Ende zählt nur diese Zahl.

## Etappen

Die erste Etappe umfasst Pegelbasis, zweiten Tap, Grenzen nach Gain Reduction, Songposition im Frame, die Kalibrierung, den Toleranzboden, die Schlussprüfung über den ganzen Song und das Speichern der Blindwahl. Sie ändert am meisten und braucht kein neues Zielmodell.

Die zweite Etappe bringt die Balance-Phase mit Dynamikprofil, die Verdeckung gegen den Restmix, Shelves und die Breite pro Band. Vorher muss der Fehlertest stehen, sonst lässt sich nicht beurteilen, ob das Dynamikprofil hilft.

Die dritte Etappe ist eine berechnete Dynamikstufe: Kompressoren mit Threshold und Ratio aus der gemessenen Pegelverteilung des Kanals, geprüft an der gemessenen Gain Reduction. Für Beats aus Samples und Synths ist sie weniger dringend als für Gesang.

## Was bleibt

Alle Schutzregeln, das Journal, A/B, die Checkpoints und die Mix-Richtung bleiben: Platz für Gesang, Master-EQ höchstens 3 dB Schnitt, ausgeschaltete Geräte unberührt, Höhen offen. Die Grenzen 5 und 4 dB bleiben als Zahlen und gelten neu für die gemessene Gain Reduction.

## Offene Entscheide

1. Bei einer Ceiling von -1 dBFS und einer Referenz bei 0 dBFS: Gleiche Lautheit und damit 1 dB dichter, oder gleiche Dichte und 1 LU leiser? Heute gilt stillschweigend das Erste. Ich würde die gleiche Dichte zum Ziel machen und die Lautheit als Folge ausweisen.
2. Der Trim als Utility auf Main oder über die Fader der obersten Ebene? Ich empfehle das Utility, weil Fader automatisiert sein können und Post-Fader-Sends mitziehen.
3. Die Pegelbasis auf dem 99. Perzentil der Peaks oder auf dem Maximum? Das Perzentil ist weniger anfällig für einen einzelnen Ausreisser, lässt aber vereinzelte Spitzen in den Clipper laufen.
