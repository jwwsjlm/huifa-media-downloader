use rmcp::{
    ErrorData, RoleServer, ServerHandler, ServiceExt,
    model::*,
    service::RequestContext,
    transport::{
        StreamableHttpClientTransport, stdio,
        streamable_http_client::StreamableHttpClientTransportConfig,
    },
};
#[derive(Clone)]
struct Bridge(rmcp::service::Peer<rmcp::RoleClient>);
impl ServerHandler for Bridge {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build())
    }
    async fn list_tools(
        &self,
        _: Option<PaginatedRequestParams>,
        _: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, ErrorData> {
        self.0
            .list_tools(None)
            .await
            .map_err(|e| ErrorData::internal_error(e.to_string(), None))
    }
    async fn call_tool(
        &self,
        args: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        let mut handle = self
            .0
            .send_cancellable_request(
                ClientRequest::CallToolRequest(CallToolRequest::new(args)),
                rmcp::service::PeerRequestOptions::no_options(),
            )
            .await
            .map_err(|e| ErrorData::internal_error(e.to_string(), None))?;
        let result = tokio::select! {
            result=&mut handle.rx=>result.map_err(|_|ErrorData::internal_error("Desktop connection closed",None))?.map_err(|e|ErrorData::internal_error(e.to_string(),None))?,
            _=context.ct.cancelled()=>{let _=handle.cancel(Some("AI client cancelled query".into())).await;return Err(ErrorData::internal_error("Query cancelled",None));}
        };
        match result {
            ServerResult::CallToolResult(result) => Ok(result.into()),
            _ => Err(ErrorData::internal_error(
                "Unexpected desktop response",
                None,
            )),
        }
    }
}
#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let _ = rustls::crypto::ring::default_provider().install_default();
    let url = std::env::var("HUIFA_MCP_URL").unwrap_or("http://127.0.0.1:19492/mcp".into());
    let parsed = reqwest::Url::parse(&url)?;
    anyhow::ensure!(
        parsed.scheme() == "http" && matches!(parsed.host_str(), Some("127.0.0.1" | "localhost")),
        "Bridge only connects to the local desktop app"
    );
    let password = std::env::var("HUIFA_MCP_PASSWORD").unwrap_or_else(|_| "admin".into());
    let config = StreamableHttpClientTransportConfig::with_uri(url).auth_header(password);
    let transport = StreamableHttpClientTransport::from_config(config);
    let client = ().serve(transport).await.map_err(|_| {
        anyhow::anyhow!(
            "Cannot connect to Huifa: start the desktop app and enable MCP; check the port"
        )
    })?;
    let server = Bridge(client.peer().clone()).serve(stdio()).await?;
    server.waiting().await?;
    client.cancel().await?;
    Ok(())
}
