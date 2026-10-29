;;; dwgdiff_dump.lsp - list every drawing entity for the DWG diff mode.
;;; scripts/dwgdiff.py copies this file to the temp folder as UTF-8 with BOM and
;;; loads it in accoreconsole. Read-only: no entmod, entmake or save command.
;;;
;;; One line per entity in model space and every paper-space layout. Fields are
;;; separated by |=| because accoreconsole processed stdout expands a princ tab:
;;;   DIFFROW|=|handle|=|type|=|layout|=|layer|=|x|=|y|=|z|=|geom|=|text|=|block
;;;   DIFFATTR|=|insert handle|=|tag|=|value
;;;   DIFFDUMP_SUMMARY|=|entity count
;;;   DIFF_ERROR|=|message
;;; geom is key=value pairs joined by ";" (sizes, radius, scale, vertices ...).
;;; Inside a field, \ newline CR tab and |=| are escaped like dwgtext.lsp.

(setq *dd-sep* "|=|")

(defun dd-str (v)
  (if (= (type v) 'STR) v ""))

(defun dd-int (v)
  (if (= (type v) 'INT) v 0))

(defun dd-get (code ed)
  (cdr (assoc code ed)))

(defun dd-esc (s / i n ch out)
  (setq s (dd-str s) i 1 n (strlen s) out "")
  (while (<= i n)
    (setq ch (substr s i 1))
    (cond
      ((= ch "\\") (setq out (strcat out "\\\\")))
      ((= ch (chr 10)) (setq out (strcat out "\\n")))
      ((= ch (chr 13)) (setq out (strcat out "\\r")))
      ((= ch (chr 9)) (setq out (strcat out "\\t")))
      ((and (<= (+ i 2) n) (= (substr s i 3) *dd-sep*))
        (setq out (strcat out "|\\=|"))
        (setq i (+ i 2)))
      (T (setq out (strcat out ch))))
    (setq i (1+ i)))
  out)

(defun dd-num (v / s i n ch out)
  (if (not (numberp v))
    ""
    (progn
      (setq s (rtos v 2 8) i 1 n (strlen s) out "")
      (while (<= i n)
        (setq ch (substr s i 1))
        (if (= ch ",") (setq ch "."))
        (setq out (strcat out ch))
        (setq i (1+ i)))
      out)))

(defun dd-kv (key v)
  (if (numberp v) (strcat key "=" (dd-num v)) ""))

(defun dd-join (items / out item)
  (setq out "")
  (foreach item items
    (if (/= item "")
      (setq out (if (= out "") item (strcat out ";" item)))))
  out)

(defun dd-lwverts (ed / out rec p)
  (setq out "")
  (foreach rec ed
    (if (= (car rec) 10)
      (progn
        (setq p (cdr rec))
        (setq out (strcat out (if (= out "") "" " ") (dd-num (car p)) "," (dd-num (cadr p)))))))
  out)

(defun dd-oldverts (en / out ed n p)
  (setq out "" n 0 en (entnext en))
  (while (and en (= (dd-str (dd-get 0 (setq ed (entget en)))) "VERTEX"))
    (setq p (dd-get 10 ed))
    (setq out (strcat out (if (= out "") "" " ")
                      (dd-num (car p)) "," (dd-num (cadr p)) "," (dd-num (caddr p))))
    (setq n (1+ n) en (entnext en)))
  (list n out))

(defun dd-geom (en ed kind / p q vv)
  (cond
    ((= kind "LINE")
      (setq p (dd-get 10 ed) q (dd-get 11 ed))
      (dd-join (list (dd-kv "x2" (car q)) (dd-kv "y2" (cadr q)) (dd-kv "z2" (caddr q))
                     (if (and p q) (dd-kv "len" (distance p q)) ""))))
    ((= kind "CIRCLE")
      (dd-join (list (dd-kv "r" (dd-get 40 ed)))))
    ((= kind "ARC")
      (dd-join (list (dd-kv "r" (dd-get 40 ed)) (dd-kv "a0" (dd-get 50 ed)) (dd-kv "a1" (dd-get 51 ed)))))
    ((= kind "ELLIPSE")
      (setq q (dd-get 11 ed))
      (dd-join (list (dd-kv "mx" (car q)) (dd-kv "my" (cadr q)) (dd-kv "ratio" (dd-get 40 ed))
                     (dd-kv "p0" (dd-get 41 ed)) (dd-kv "p1" (dd-get 42 ed)))))
    ((member kind '("TEXT" "ATTDEF"))
      (dd-join (list (dd-kv "h" (dd-get 40 ed)) (dd-kv "rot" (dd-get 50 ed)) (dd-kv "wf" (dd-get 41 ed)))))
    ((= kind "MTEXT")
      (dd-join (list (dd-kv "h" (dd-get 40 ed)) (dd-kv "w" (dd-get 41 ed)) (dd-kv "rot" (dd-get 50 ed)))))
    ((= kind "INSERT")
      (dd-join (list (dd-kv "sx" (dd-get 41 ed)) (dd-kv "sy" (dd-get 42 ed)) (dd-kv "sz" (dd-get 43 ed))
                     (dd-kv "rot" (dd-get 50 ed)))))
    ((= kind "DIMENSION")
      (dd-join (list (dd-kv "meas" (dd-get 42 ed)))))
    ((= kind "LWPOLYLINE")
      (dd-join (list (dd-kv "n" (dd-get 90 ed)) (dd-kv "closed" (logand (dd-int (dd-get 70 ed)) 1))
                     (strcat "verts=" (dd-lwverts ed)))))
    ((= kind "POLYLINE")
      (setq vv (dd-oldverts en))
      (dd-join (list (dd-kv "n" (car vv)) (dd-kv "closed" (logand (dd-int (dd-get 70 ed)) 1))
                     (strcat "verts=" (cadr vv)))))
    ((= kind "HATCH")
      (dd-join (list (strcat "pat=" (dd-str (dd-get 2 ed))) (dd-kv "scale" (dd-get 41 ed))
                     (dd-kv "ang" (dd-get 52 ed)) (dd-kv "loops" (dd-get 91 ed)))))
    ((= kind "SPLINE")
      (dd-join (list (dd-kv "deg" (dd-get 71 ed)) (dd-kv "nctl" (dd-get 73 ed)) (dd-kv "nfit" (dd-get 74 ed)))))
    (T "")))

(defun dd-text (ed kind / out rec)
  (setq out "")
  (cond
    ((= kind "MTEXT")
      (foreach rec ed
        (if (member (car rec) '(1 3))
          (setq out (strcat out (dd-str (cdr rec)))))))
    ((member kind '("TEXT" "ATTDEF" "DIMENSION"))
      (setq out (dd-str (dd-get 1 ed))))
    ((= kind "MULTILEADER")
      (setq out (dd-str (dd-get 304 ed)))))
  out)

(defun dd-attrs (en handle / ed)
  (setq en (entnext en))
  (while (and en (= (dd-str (dd-get 0 (setq ed (entget en)))) "ATTRIB"))
    (princ (strcat "\nDIFFATTR" *dd-sep* (dd-esc handle)
                   *dd-sep* (dd-esc (dd-get 2 ed))
                   *dd-sep* (dd-esc (dd-get 1 ed)) "\n"))
    (setq en (entnext en)))
  (princ))

(defun dd-row (en / ed kind handle layout p z block)
  (setq ed (entget en)
        kind (dd-str (dd-get 0 ed))
        handle (dd-str (dd-get 5 ed))
        layout (dd-str (dd-get 410 ed))
        p (dd-get 10 ed)
        block (if (= kind "INSERT") (dd-str (dd-get 2 ed)) ""))
  (if (= layout "") (setq layout "Model"))
  (if (not (listp p)) (setq p nil))
  (setq z (if (caddr p) (caddr p) (dd-get 38 ed)))
  (princ (strcat "\nDIFFROW" *dd-sep* (dd-esc handle)
                 *dd-sep* (dd-esc kind)
                 *dd-sep* (dd-esc layout)
                 *dd-sep* (dd-esc (dd-get 8 ed))
                 *dd-sep* (dd-num (car p))
                 *dd-sep* (dd-num (cadr p))
                 *dd-sep* (dd-num z)
                 *dd-sep* (dd-esc (dd-geom en ed kind))
                 *dd-sep* (dd-esc (dd-text ed kind))
                 *dd-sep* (dd-esc block)
                 "\n"))
  (if (and (= kind "INSERT") (= (dd-get 66 ed) 1))
    (dd-attrs en handle))
  (princ))

(defun dd-dump-current (/ ss i n olderr)
  (setq olderr *error* n 0 i 0)
  (defun *error* (msg)
    (princ (strcat "\nDIFF_ERROR" *dd-sep* (dd-esc msg) "\n"))
    (princ (strcat "\nDIFFDUMP_SUMMARY" *dd-sep* "0\n"))
    (setq *error* olderr)
    (princ))
  (setq ss (ssget "_X"))
  (if ss
    (progn
      (setq n (sslength ss))
      (while (< i n)
        (dd-row (ssname ss i))
        (setq i (1+ i)))))
  (princ (strcat "\nDIFFDUMP_SUMMARY" *dd-sep* (itoa n) "\n"))
  (setq *error* olderr)
  (princ))

(princ "\nDIFFDUMP_READY\n")
(princ)
