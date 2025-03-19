git pull
helm uninstall e2mgr
helm uninstall e2term
helm uninstall appmgr
helm uninstall dbaas
helm uninstall submgr
helm uninstall xapp
helm uninstall rtmgr
helm install dbaas charts/dbaas
helm install e2mgr charts/e2mgr
helm install e2term charts/e2term
helm install xapp charts/python_xapp_runner
helm install appmgr charts/appmgr
helm install submgr charts/submgr
helm install rtmgr charts/rtmgr
