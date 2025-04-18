git pull
helm uninstall e2mgr
helm uninstall e2term
helm uninstall appmgr
helm uninstall dbaas
helm uninstall submgr
helm uninstall xapp
helm uninstall rtmgr-sim
helm install dbaas charts/dbaas
sleep 5
helm install rtmgr-sim charts/rtmgr-sim
sleep 5
helm install submgr charts/submgr
helm install e2term charts/e2term
helm install appmgr charts/appmgr
helm install e2mgr charts/e2mgr
helm install xapp charts/python_xapp_runner
