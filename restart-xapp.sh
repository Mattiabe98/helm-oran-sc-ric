git pull
helm uninstall xapp
sleep 5
helm install xapp charts/python_xapp_runner
