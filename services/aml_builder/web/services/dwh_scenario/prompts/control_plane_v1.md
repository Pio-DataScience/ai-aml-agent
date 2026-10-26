The canonical AMLIntent and confirmed filters are authoritative. SQL must use confirmed filter
values, honor explicit EXPL_CODE semantics, and preserve rolling and baseline window meaning.
Service A's SQL policy is authoritative for read-only safety, object scope, bind values, and
required CUS_NUM projection. Critique is advisory and cannot authorize a policy failure.
